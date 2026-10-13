import json
from dataclasses import replace

import pytest
from sqlalchemy import func, select

from fsm_license.crypto import activation_key_hash, b64decode, b64encode
from fsm_license.database import Activation, Entitlement
from fsm_panel.licensing import DeviceIdentity

from .conftest import signed_request


def test_service_auth_revision_idempotency_and_hash_only_credentials(service):
    assert service.client.put("/internal/v1/entitlements/order-1", json={}).status_code == 401
    initial = service.put()
    assert initial.status_code == 200
    key = initial.json()["activation_key"]
    assert len(key) >= 43 and initial.json()["changed"]
    assert initial.headers["cache-control"] == "no-store"
    with service.db.sessions() as session:
        item = session.scalar(select(Entitlement))
        assert item.activation_key_hash == activation_key_hash(key)
        assert key not in repr(item.__dict__)
    repeated = service.put().json()
    assert repeated["changed"] is False and repeated["activation_key"] is None
    assert service.put(device_limit=2).status_code == 409
    assert service.put(revision=3).status_code == 200
    assert service.put(revision=2, status="revoked").status_code == 409
    assert service.put(revision=3).json()["status"] == "active"


def test_activation_limit_reuse_installation_binding_and_release(service):
    key = service.put().json()["activation_key"]
    device, first = service.activate(key)
    assert first.status_code == 201
    activation_id = first.json()["activation_id"]
    _, repeat = service.activate(key, device)
    assert repeat.status_code == 201 and repeat.json()["activation_id"] == activation_id
    assert service.activate(key)[1].status_code == 409
    impostor = replace(DeviceIdentity.generate(), installation_id=device.installation_id)
    assert service.activate(key, impostor)[1].status_code == 409
    assert service.public(device, "DELETE", f"/v1/activations/{activation_id}").status_code == 204
    assert service.public(device, "DELETE", f"/v1/activations/{activation_id}").status_code == 204
    assert (
        service.public(
            device,
            "POST",
            "/v1/leases/renew",
            {
                "activation_id": activation_id,
            },
        ).status_code
        == 403
    )
    assert service.activate(key)[1].status_code == 201
    with service.db.sessions() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(Activation).where(Activation.released_at.is_(None))
            )
            == 1
        )


def test_proof_required_signed_over_method_target_body_and_consumed_once(service):
    key = service.put().json()["activation_key"]
    device = DeviceIdentity.generate()
    payload = {
        "activation_key": key,
        "installation_id": device.installation_id,
        "device_public_key": b64encode(device.public_key),
    }
    assert service.client.post("/v1/activations", json=payload).status_code == 401
    body, headers = signed_request(device, service.clock.wall, "POST", "/v1/activations", payload)
    response = service.client.post("/v1/activations", content=body, headers=headers)
    assert response.status_code == 201
    assert service.client.post("/v1/activations", content=body, headers=headers).status_code == 409
    altered = body.replace(
        device.installation_id.encode(), DeviceIdentity.generate().installation_id.encode()
    )
    assert (
        service.client.post("/v1/activations", content=altered, headers=headers).status_code == 401
    )
    assert (
        service.public(
            device, "POST", "/v1/activations", payload, timestamp=int(service.clock.wall) - 31
        ).status_code
        == 401
    )
    activation_id = response.json()["activation_id"]
    bad = DeviceIdentity.generate()
    assert (
        service.public(
            bad,
            "POST",
            "/v1/leases/renew",
            {
                "activation_id": activation_id,
            },
        ).status_code
        == 401
    )
    target = f"/v1/license/current?activation_id={activation_id}"
    body, headers = signed_request(device, service.clock.wall, "POST", target)
    assert service.client.get(target, headers=headers).status_code == 401
    assert service.public(device, "GET", target).status_code == 200


def test_expiry_revocation_and_activation_key_rotation(service):
    key = service.put(expires_at="2030-01-01T00:00:17Z").json()["activation_key"]
    device, response = service.activate(key)
    activation_id = response.json()["activation_id"]
    claims = json.loads(b64decode(response.json()["lease"].split(".")[1]))
    assert claims["exp"] - claims["iat"] == 17
    service.clock.advance(17)
    assert (
        service.public(
            device,
            "POST",
            "/v1/leases/renew",
            {
                "activation_id": activation_id,
            },
        ).status_code
        == 403
    )
    assert service.activate(key)[1].status_code == 403
    rotated = service.put(revision=2, rotate_activation_key=True)
    new_key = rotated.json()["activation_key"]
    assert (
        new_key != key
        and service.put(revision=2, rotate_activation_key=True).json()["activation_key"] is None
    )
    assert service.activate(key)[1].status_code == 401
    assert (
        service.public(
            device,
            "POST",
            "/v1/leases/renew",
            {
                "activation_id": activation_id,
            },
        ).status_code
        == 200
    )
    assert service.put(revision=3, status="revoked").status_code == 200
    assert (
        service.public(
            device,
            "POST",
            "/v1/leases/renew",
            {
                "activation_id": activation_id,
            },
        ).status_code
        == 403
    )
    # Device release remains possible after revocation or expiry.
    assert service.public(device, "DELETE", f"/v1/activations/{activation_id}").status_code == 204


def test_limit_decrease_releases_newest_seats_and_admin_transfers_lost_device(service):
    key = service.put(device_limit=2).json()["activation_key"]
    old, first = service.activate(key)
    service.clock.advance(1)
    new, second = service.activate(key)
    first_id, second_id = first.json()["activation_id"], second.json()["activation_id"]
    assert service.put(revision=2).json()["active_devices"] == 1
    assert (
        service.public(new, "POST", "/v1/leases/renew", {"activation_id": second_id}).status_code
        == 403
    )
    assert (
        service.public(old, "POST", "/v1/leases/renew", {"activation_id": first_id}).status_code
        == 200
    )
    target = "/internal/v1/entitlements/order-1/activations"
    assert service.client.get(target).status_code == 401
    devices = service.client.get(target, headers=service.auth).json()
    assert len(devices) == 2 and devices[1]["release_reason"] == "device_limit_reduced"
    assert service.client.delete(target + "/" + first_id, headers=service.auth).status_code == 204
    assert service.client.delete(target + "/" + first_id, headers=service.auth).status_code == 204
    assert service.activate(key)[1].status_code == 201


@pytest.mark.parametrize(
    "changes",
    [
        {"expires_at": "2030-01-01T00:00:00"},
        {"device_limit": 0},
        {"revision": 0},
        {"device_limit": True},
        {"unexpected": 1},
        {"status": "paid"},
    ],
)
def test_invalid_entitlement_contract_is_rejected(service, changes):
    assert service.put(**changes).status_code == 422


def test_openapi_and_health(service):
    assert service.client.get("/healthz").json() == {"status": "ok"}
    paths = service.app.openapi()["paths"]
    assert {
        "/internal/v1/entitlements/{external_id}",
        "/v1/activations",
        "/v1/leases/renew",
        "/v1/activations/{activation_id}",
        "/v1/license/current",
    } <= set(paths)
