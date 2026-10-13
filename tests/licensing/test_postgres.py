"""Real row-lock tests. Each run owns and removes only a newly generated test schema."""

import os
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url

from alembic import command
from fsm_license.app import create_app
from fsm_license.config import Settings
from fsm_license.crypto import b64encode
from fsm_license.database import Activation, Database, Entitlement
from fsm_panel.licensing import DeviceIdentity

from .conftest import Clock, Service, signed_request

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg_service():
    configured = os.environ.get("FSM_TEST_POSTGRES_URL")
    if not configured:
        pytest.skip(
            "FSM_TEST_POSTGRES_URL absent; SQLite cannot prove PostgreSQL row-lock behavior"
        )
    url = make_url(configured)
    if url.drivername != "postgresql+psycopg":
        pytest.fail("FSM_TEST_POSTGRES_URL must use postgresql+psycopg")
    schema = "fsm_license_test_" + uuid4().hex
    control = create_engine(url)
    with control.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped_url = url.update_query_dict({"options": f"-csearch_path={schema}"})
    db = Database(engine=create_engine(scoped_url, pool_size=12, max_overflow=4))
    try:
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        config.attributes["database_url"] = scoped_url.render_as_string(hide_password=False)
        command.upgrade(config, "head")
        clock = Clock()
        settings = Settings(
            database_url=scoped_url.render_as_string(hide_password=False),
            internal_token=secrets.token_urlsafe(48),
            signing_key=Ed25519PrivateKey.generate().private_bytes_raw(),
        )
        app = create_app(settings, db, clock=clock.now)
        with TestClient(app) as client:
            yield Service(client, app, db, settings, clock)
    finally:
        db.dispose()
        with control.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        control.dispose()


def test_simultaneous_activation_never_oversubscribes_seats(pg_service):
    key = pg_service.put().json()["activation_key"]
    barrier = threading.Barrier(8)

    def activate(_):
        barrier.wait(timeout=10)
        return pg_service.activate(key)[1].status_code

    with ThreadPoolExecutor(max_workers=8) as workers:
        statuses = list(workers.map(activate, range(8)))
    assert statuses.count(201) == 1 and statuses.count(409) == 7
    with pg_service.db.sessions() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(Activation).where(Activation.released_at.is_(None))
            )
            == 1
        )


def test_simultaneous_first_provisioning_is_idempotent(pg_service):
    barrier = threading.Barrier(6)

    def provision(_):
        barrier.wait(timeout=10)
        return pg_service.put()

    with ThreadPoolExecutor(max_workers=6) as workers:
        results = list(workers.map(provision, range(6)))
    assert all(result.status_code == 200 for result in results)
    assert sum(result.json()["activation_key"] is not None for result in results) == 1
    with pg_service.db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Entitlement)) == 1


def test_concurrent_revisions_cannot_resurrect_revoked_entitlement(pg_service):
    pg_service.put()
    barrier = threading.Barrier(2)

    def update(revision):
        barrier.wait(timeout=10)
        return pg_service.put(revision=revision, status="revoked" if revision == 3 else "active")

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(update, [2, 3]))
    assert results[1].status_code == 200
    assert results[0].status_code in (200, 409)
    with pg_service.db.sessions() as session:
        item = session.scalar(select(Entitlement))
        assert item.revision == 3 and item.status == "revoked"


def test_concurrent_identical_proof_cannot_create_two_activations(pg_service):
    key = pg_service.put(device_limit=2).json()["activation_key"]
    device = DeviceIdentity.generate()
    body, headers = signed_request(
        device,
        pg_service.clock.wall,
        "POST",
        "/v1/activations",
        {
            "activation_key": key,
            "installation_id": device.installation_id,
            "device_public_key": b64encode(device.public_key),
        },
    )
    barrier = threading.Barrier(2)

    def activate(_):
        barrier.wait(timeout=10)
        return pg_service.client.post("/v1/activations", content=body, headers=headers).status_code

    with ThreadPoolExecutor(max_workers=2) as workers:
        statuses = list(workers.map(activate, range(2)))
    assert sorted(statuses) == [201, 409]
