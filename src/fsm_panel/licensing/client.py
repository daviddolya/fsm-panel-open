"""Async HTTP client; no payment integration and no persisted/offline lease fallback."""

import asyncio
import ipaddress
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fsm_license.crypto import b64encode, canonical_json, key_fingerprint, proof_message

from .device import DeviceIdentity, DeviceStore
from .gate import LocalLeaseGate
from .models import LeaseClaims, LicenseUnavailable


class LicenseClient:
    def __init__(
        self,
        *,
        base_url: str,
        store: DeviceStore,
        public_keys: Mapping[str, bytes],
        issuer: str,
        audience: str,
        http_client: httpx.AsyncClient | None = None,
        allow_insecure_http: bool = False,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        url = urlsplit(base_url)
        try:
            loopback = ipaddress.ip_address(url.hostname or "").is_loopback
        except ValueError:
            loopback = url.hostname == "localhost"
        if url.scheme != "https" and not (
            allow_insecure_http and url.scheme == "http" and loopback
        ):
            raise ValueError("license service requires HTTPS")
        if url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
            raise ValueError("license service URL must be an origin")
        if not url.hostname:
            raise ValueError("license service URL requires a host")
        self._base_url = base_url.rstrip("/")
        self._store = store
        self._identity = store.load() or DeviceIdentity.generate()
        store.save(self._identity)
        self._http = http_client or httpx.AsyncClient(timeout=10, follow_redirects=False)
        self._owns_http = http_client is None
        self._wall, self._monotonic = wall_clock, monotonic_clock
        self._lock = asyncio.Lock()
        self.gate = LocalLeaseGate(
            public_keys=public_keys,
            issuer=issuer,
            audience=audience,
            installation_id=self._identity.installation_id,
            device_key_sha256=key_fingerprint(self._identity.public_key),
            wall_clock=wall_clock,
            monotonic_clock=monotonic_clock,
        )

    @property
    def identity(self) -> DeviceIdentity:
        return self._identity

    async def _request(self, method: str, target: str, payload: dict | None = None):
        body = canonical_json(payload) if payload is not None else b""
        timestamp, nonce = str(int(self._wall())), secrets.token_urlsafe(32)
        signature = Ed25519PrivateKey.from_private_bytes(self._identity.private_key).sign(
            proof_message(method, target, timestamp, nonce, body)
        )
        headers = {
            "X-Device-Timestamp": timestamp,
            "X-Device-Nonce": nonce,
            "X-Device-Signature": b64encode(signature),
            "Content-Type": "application/json",
        }
        started = self._monotonic()
        try:
            response = await self._http.request(
                method,
                self._base_url + target,
                content=body,
                headers=headers,
                timeout=10,
                follow_redirects=False,
            )
            if response.status_code >= 400 or response.is_redirect:
                detail = "license service denied the request"
                try:
                    candidate = response.json().get("detail")
                    if isinstance(candidate, str):
                        detail = candidate[:200]
                except ValueError, AttributeError:
                    pass
                raise LicenseUnavailable(detail)
            return response, nonce, started
        except (httpx.HTTPError, LicenseUnavailable) as exc:
            self.gate.invalidate("online license verification failed")
            if isinstance(exc, LicenseUnavailable):
                raise
            raise LicenseUnavailable("license service unreachable") from exc
        except asyncio.CancelledError:
            self.gate.invalidate("license request cancelled")
            raise

    def _accept(self, response: httpx.Response, nonce: str, started: float, activation_id=None):
        try:
            item = response.json()
            received_id = str(UUID(item["activation_id"]))
            if activation_id is not None and received_id != activation_id:
                raise ValueError("activation changed unexpectedly")
            claims = self.gate.accept(
                item["lease"],
                activation_id=received_id,
                request_nonce=nonce,
                request_started_monotonic=started,
            )
            if received_id != self._identity.activation_id:
                identity = replace(self._identity, activation_id=received_id)
                self._store.save(identity)
                self._identity = identity
            return claims
        except Exception as exc:
            self.gate.invalidate("invalid or unpersisted license response")
            raise LicenseUnavailable("invalid or unpersisted license response") from exc

    async def activate(self, activation_key: str) -> LeaseClaims:
        async with self._lock:
            response, nonce, started = await self._request(
                "POST",
                "/v1/activations",
                {
                    "activation_key": activation_key,
                    "installation_id": self._identity.installation_id,
                    "device_public_key": b64encode(self._identity.public_key),
                },
            )
            return self._accept(response, nonce, started)

    async def renew(self) -> LeaseClaims:
        async with self._lock:
            activation_id = self._identity.activation_id
            if activation_id is None:
                self.gate.invalidate("activation required")
                raise LicenseUnavailable("activation required")
            response, nonce, started = await self._request(
                "POST", "/v1/leases/renew", {"activation_id": activation_id}
            )
            return self._accept(response, nonce, started, activation_id)

    async def current(self) -> LeaseClaims:
        async with self._lock:
            activation_id = self._identity.activation_id
            if activation_id is None:
                raise LicenseUnavailable("activation required")
            response, nonce, started = await self._request(
                "GET", f"/v1/license/current?activation_id={activation_id}"
            )
            return self._accept(response, nonce, started, activation_id)

    async def deactivate(self) -> None:
        async with self._lock:
            self.gate.invalidate("activation released")
            if self._identity.activation_id is None:
                return
            await self._request("DELETE", f"/v1/activations/{self._identity.activation_id}")
            identity = replace(self._identity, activation_id=None)
            self._store.save(identity)
            self._identity = identity

    async def run_renewal(self, stop_event: asyncio.Event) -> None:
        """Refresh immediately on startup, then every 30 seconds; retry while disconnected."""
        try:
            while not stop_event.is_set():
                try:
                    await self.renew()
                except LicenseUnavailable:
                    pass  # gate is already closed; next iteration attempts recovery
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=30)
                except TimeoutError:
                    pass
        finally:
            self.gate.invalidate("license renewal stopped")

    async def aclose(self) -> None:
        self.gate.invalidate("license client closed")
        if self._owns_http:
            await self._http.aclose()
