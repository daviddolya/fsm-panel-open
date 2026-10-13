import asyncio

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fsm_panel.licensing import LicenseClient, LicenseUnavailable, MemoryDeviceStore


class SwitchableTransport(httpx.AsyncBaseTransport):
    def __init__(self, app):
        self.inner = httpx.ASGITransport(app=app)
        self.offline = False
        self.corrupt = False

    async def handle_async_request(self, request):
        if self.offline:
            raise httpx.ConnectError("synthetic network failure", request=request)
        response = await self.inner.handle_async_request(request)
        if self.corrupt:
            return httpx.Response(200, json={"activation_id": 123, "lease": "broken"})
        return response


def make_client(service, http_client, store=None, **options):
    return LicenseClient(
        base_url="http://127.0.0.1",
        store=store or MemoryDeviceStore(),
        public_keys={
            service.settings.key_id: Ed25519PrivateKey.from_private_bytes(
                service.settings.signing_key
            )
            .public_key()
            .public_bytes_raw()
        },
        issuer=service.settings.issuer,
        audience=service.settings.audience,
        http_client=http_client,
        allow_insecure_http=True,
        wall_clock=lambda: service.clock.wall,
        monotonic_clock=lambda: service.clock.monotonic,
        **options,
    )


@pytest.mark.asyncio
async def test_real_http_contract_activate_renew_current_restart_and_deactivate(service):
    key = service.put().json()["activation_key"]
    store = MemoryDeviceStore()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service.app)) as http:
        client = make_client(service, http, store)
        claims = await client.activate(key)
        assert client.gate.require_valid() == claims
        assert key not in repr(store.load())
        assert "private_key" not in repr(client.identity)
        service.clock.advance(30)
        assert (await client.renew()).issued_at > claims.issued_at
        assert (await client.current()).activation_id == claims.activation_id
        restarted = make_client(service, http, store)
        with pytest.raises(LicenseUnavailable):
            restarted.gate.require_valid()
        assert (await restarted.renew()).activation_id == claims.activation_id
        await restarted.deactivate()
        assert restarted.identity.activation_id is None
        with pytest.raises(LicenseUnavailable):
            restarted.gate.require_valid()
        with pytest.raises(LicenseUnavailable):
            await client.renew()


@pytest.mark.asyncio
async def test_network_failure_immediately_closes_gate_then_online_recovers(service):
    key = service.put().json()["activation_key"]
    transport = SwitchableTransport(service.app)
    async with httpx.AsyncClient(transport=transport) as http:
        client = make_client(service, http)
        await client.activate(key)
        transport.offline = True
        with pytest.raises(LicenseUnavailable, match="unreachable"):
            await client.renew()
        with pytest.raises(LicenseUnavailable):
            client.gate.require_valid()
        transport.offline = False
        await client.renew()
        assert client.gate.require_valid()
        transport.corrupt = True
        with pytest.raises(LicenseUnavailable):
            await client.renew()
        with pytest.raises(LicenseUnavailable):
            client.gate.require_valid()


@pytest.mark.asyncio
async def test_revocation_closes_gate_and_release_failure_keeps_retry_identity(service):
    key = service.put().json()["activation_key"]
    transport = SwitchableTransport(service.app)
    async with httpx.AsyncClient(transport=transport) as http:
        client = make_client(service, http)
        await client.activate(key)
        activation_id = client.identity.activation_id
        service.put(revision=2, status="revoked")
        with pytest.raises(LicenseUnavailable, match="revoked"):
            await client.renew()
        with pytest.raises(LicenseUnavailable):
            client.gate.require_valid()
        transport.offline = True
        with pytest.raises(LicenseUnavailable):
            await client.deactivate()
        assert client.identity.activation_id == activation_id
        transport.offline = False
        await client.deactivate()
        assert client.identity.activation_id is None


@pytest.mark.asyncio
async def test_background_renewal_immediate_then_30_seconds_and_stop_closes_gate(
    service, monkeypatch
):
    key = service.put().json()["activation_key"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service.app)) as http:
        client = make_client(service, http)
        await client.activate(key)
        stop = asyncio.Event()
        calls = []

        async def wait_for(awaitable, timeout):
            calls.append(timeout)
            assert client.gate.require_valid()
            stop.set()
            return await awaitable

        monkeypatch.setattr("fsm_panel.licensing.client.asyncio.wait_for", wait_for)
        await client.run_renewal(stop)
        assert calls == [30]
        with pytest.raises(LicenseUnavailable):
            client.gate.require_valid()


@pytest.mark.parametrize(
    "url,override",
    [
        ("http://license.example.invalid", False),
        ("http://license.example.invalid", True),
        ("https://user:password@license.example.invalid", False),
    ],
)
def test_plaintext_remote_urls_and_url_credentials_are_rejected(url, override):
    with pytest.raises(ValueError):
        LicenseClient(
            base_url=url,
            store=MemoryDeviceStore(),
            public_keys={},
            issuer="test",
            audience="test",
            allow_insecure_http=override,
        )
