import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from fsm_license.app import create_app
from fsm_license.config import Settings
from fsm_license.crypto import b64encode, canonical_json, proof_message
from fsm_license.database import Base, Database
from fsm_panel.licensing import DeviceIdentity


@dataclass
class Clock:
    wall: float = 1893456000.0  # Synthetic 2030-01-01 UTC.
    monotonic: float = 1000.0

    def now(self):
        return datetime.fromtimestamp(self.wall, UTC)

    def advance(self, seconds):
        self.wall += seconds
        self.monotonic += seconds


@dataclass
class Service:
    client: TestClient
    app: object
    db: Database
    settings: Settings
    clock: Clock

    @property
    def auth(self):
        return {"Authorization": f"Bearer {self.settings.internal_token}"}

    def put(self, external_id="order-1", **changes):
        payload = dict(
            external_customer_id="synthetic-customer",
            expires_at="2030-02-01T00:00:00Z",
            device_limit=1,
            status="active",
            revision=1,
        )
        payload.update(changes)
        return self.client.put(
            f"/internal/v1/entitlements/{external_id}", json=payload, headers=self.auth
        )

    def public(self, device, method, target, payload=None, **kwargs):
        body, headers = signed_request(device, self.clock.wall, method, target, payload, **kwargs)
        return self.client.request(method, target, content=body, headers=headers)

    def activate(self, key, device=None):
        device = device or DeviceIdentity.generate()
        response = self.public(
            device,
            "POST",
            "/v1/activations",
            {
                "activation_key": key,
                "installation_id": device.installation_id,
                "device_public_key": b64encode(device.public_key),
            },
        )
        return device, response


def signed_request(device, wall, method, target, payload=None, *, nonce=None, timestamp=None):
    body = canonical_json(payload) if payload is not None else b""
    timestamp = str(int(wall)) if timestamp is None else str(timestamp)
    nonce = nonce or secrets.token_urlsafe(32)
    signature = Ed25519PrivateKey.from_private_bytes(device.private_key).sign(
        proof_message(method, target, timestamp, nonce, body)
    )
    return body, {
        "Content-Type": "application/json",
        "X-Device-Timestamp": timestamp,
        "X-Device-Nonce": nonce,
        "X-Device-Signature": b64encode(signature),
    }


@pytest.fixture
def service():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    db = Database(engine=engine)
    clock = Clock()
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        internal_token=secrets.token_urlsafe(48),
        signing_key=Ed25519PrivateKey.generate().private_bytes_raw(),
    )
    app = create_app(settings, db, clock=clock.now)
    with TestClient(app) as client:
        yield Service(client, app, db, settings, clock)
    db.dispose()
