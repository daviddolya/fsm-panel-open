"""Local Docker smoke check; creates only a temporary service container with test keys.

Requires a disposable PostgreSQL database reachable from Docker at --database-url.
Runs real Alembic migration + HTTP activation/revocation, then removes its container.
Do not point this development check at a production database.
"""

import argparse
import asyncio
import json
import os
import secrets
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fsm_license.crypto import b64encode
from fsm_panel.licensing import LicenseClient, LicenseUnavailable, MemoryDeviceStore


async def check(url: str, token: str, key: Ed25519PrivateKey) -> dict:
    async with httpx.AsyncClient(base_url=url, timeout=5) as http:
        for _ in range(100):
            try:
                response = await http.get("/openapi.json")
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)
        else:
            raise RuntimeError("container_http_start_timeout")
        entitlement = {
            "external_customer_id": "synthetic-container-customer",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "device_limit": 1,
            "status": "active",
            "revision": 1,
        }
        target = "/internal/v1/entitlements/test-" + uuid4().hex
        auth = {"Authorization": f"Bearer {token}"}
        response = await http.put(target, json=entitlement, headers=auth)
        response.raise_for_status()
        activation_key = response.json()["activation_key"]
        client = LicenseClient(
            base_url=url,
            store=MemoryDeviceStore(),
            public_keys={"container-test": key.public_key().public_bytes_raw()},
            issuer="container-test",
            audience="fsm-panel-test",
            allow_insecure_http=True,
        )
        try:
            claims = await client.activate(activation_key)
            assert client.gate.require_valid() == claims
            assert claims.expires_at - claims.issued_at <= 60
            await client.renew()
            response = await http.put(
                target, json={**entitlement, "revision": 2, "status": "revoked"}, headers=auth
            )
            response.raise_for_status()
            try:
                await client.renew()
            except LicenseUnavailable:
                pass
            else:
                raise AssertionError("revocation did not block renewal")
            await client.deactivate()
        finally:
            await client.aclose()
        return {"migration": "passed", "real_http_activation_renewal_revocation": "passed"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--image", default="fsm-license:research")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    name = "fsm-license-smoke-" + uuid4().hex[:12]
    key = Ed25519PrivateKey.generate()
    token = secrets.token_urlsafe(48)
    values = {
        "FSM_LICENSE_DATABASE_URL": args.database_url,
        "FSM_LICENSE_INTERNAL_TOKEN": token,
        "FSM_LICENSE_SIGNING_KEY": b64encode(key.private_bytes_raw()),
        "FSM_LICENSE_ISSUER": "container-test",
        "FSM_LICENSE_AUDIENCE": "fsm-panel-test",
        "FSM_LICENSE_KEY_ID": "container-test",
    }
    command = ["docker", "run", "--detach", "--name", name, "-p", "127.0.0.1::8766"]
    for variable in values:
        command += ["--env", variable]
    started = time.monotonic()
    try:
        subprocess.run(
            command + [args.image],
            env={**os.environ, **values},
            check=True,
            capture_output=True,
            text=True,
        )
        port = (
            subprocess.check_output(["docker", "port", name, "8766/tcp"], text=True)
            .strip()
            .split(":")[-1]
        )
        result = asyncio.run(check(f"http://127.0.0.1:{port}", token, key))
        result.update(
            {
                "image": args.image,
                "seconds": round(time.monotonic() - started, 3),
                "production_tls_tested": False,
            }
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result))
    finally:
        subprocess.run(["docker", "rm", "--force", name], capture_output=True, check=False)


if __name__ == "__main__":
    main()
