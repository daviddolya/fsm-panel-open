import asyncio
import secrets

import httpx
import pytest
from fastapi.testclient import TestClient
from licensing.test_client import make_client
from licensing.test_gate import case as case

from fsm_license.crypto import b64encode
from fsm_panel.licensing import LicenseUnavailable


def test_untrusted_deep_header_closes_previously_valid_gate(case):
    case.accept()
    # Parsing the untrusted JWS header happens before signature verification. Stay
    # below the documented 16 KiB token cap, but exceed the JSON parser depth.
    header = b64encode(("[" * 1500 + "0" + "]" * 1500).encode())
    with pytest.raises(LicenseUnavailable):
        case.accept(f"{header}.e30.AA")
    with pytest.raises(LicenseUnavailable):
        case.gate.require_valid()


@pytest.mark.parametrize("changed", [{"aud": "another-app"}, {"iss": "another-issuer"}])
def test_validly_signed_foreign_scope_cannot_replace_a_live_lease(case, changed):
    case.accept()
    case.payload["request_nonce"] = secrets.token_urlsafe(32)
    with pytest.raises(LicenseUnavailable):
        case.accept(case.token(**changed))
    with pytest.raises(LicenseUnavailable):
        case.gate.require_valid()


async def test_cancellation_of_dispatched_renewal_closes_gate(service):
    entered = asyncio.Event()
    release = asyncio.Event()
    inner = httpx.ASGITransport(app=service.app)

    class DelayedRenewal(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path == "/v1/leases/renew":
                entered.set()
                await release.wait()
            return await inner.handle_async_request(request)

    key = service.put().json()["activation_key"]
    async with httpx.AsyncClient(transport=DelayedRenewal()) as http:
        client = make_client(service, http)
        await client.activate(key)
        task = asyncio.create_task(client.renew())
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(LicenseUnavailable):
            client.gate.require_valid()
        release.set()
        assert await client.renew()


async def test_limit_reduction_keeps_old_lease_bounded_and_revocation_closes_on_renew(service):
    key = service.put(device_limit=2).json()["activation_key"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service.app)) as http:
        first = make_client(service, http)
        second = make_client(service, http)
        await first.activate(key)
        service.clock.advance(1)
        await second.activate(key)
        assert service.put(device_limit=1, revision=2).status_code == 200
        # Issued leases do not claim instantaneous server-pushed revocation.
        assert second.gate.require_valid()
        with pytest.raises(LicenseUnavailable):
            await second.renew()
        with pytest.raises(LicenseUnavailable):
            second.gate.require_valid()
        assert (await first.renew()).device_limit == 1
        assert service.put(device_limit=1, status="revoked", revision=3).status_code == 200
        service.clock.advance(60)
        with pytest.raises(LicenseUnavailable):
            first.gate.require_valid()
        # Recovery/release has no lease precondition, even after revocation.
        await first.deactivate()
        await second.deactivate()


@pytest.mark.postgres
@pytest.mark.parametrize("revision", [2**31, 10**100])
def test_large_revision_is_supported_or_rejected_before_database_failure(pg_service, revision):
    payload = {
        "external_customer_id": "synthetic_revision_range",
        "expires_at": "2030-02-01T00:00:00Z",
        "device_limit": 1,
        "status": "active",
        "revision": revision,
    }
    with TestClient(pg_service.app, raise_server_exceptions=False) as client:
        response = client.put(
            "/internal/v1/entitlements/synthetic-large-revision",
            json=payload,
            headers=pg_service.auth,
        )
    assert response.status_code in {200, 422}
    if response.status_code == 200:
        assert response.json()["revision"] == revision
