import secrets
from dataclasses import dataclass

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fsm_license.crypto import key_fingerprint, sign_lease
from fsm_panel.licensing import DeviceIdentity, LicenseUnavailable, LocalLeaseGate

from .conftest import Clock


@dataclass
class GateCase:
    gate: LocalLeaseGate
    clock: Clock
    key: bytes
    payload: dict

    def token(self, **overrides):
        return sign_lease(self.payload | overrides, self.key, "test-key")

    def accept(self, token=None, **kwargs):
        args = dict(
            activation_id=self.payload["activation_id"],
            request_nonce=self.payload["request_nonce"],
            request_started_monotonic=self.clock.monotonic,
        )
        args.update(kwargs)
        return self.gate.accept(token or self.token(), **args)


@pytest.fixture
def case():
    clock = Clock()
    device = DeviceIdentity.generate()
    signer = Ed25519PrivateKey.generate()
    payload = dict(
        v=1,
        iss="test-issuer",
        aud="test-panel",
        sub="test-entitlement",
        activation_id="test-activation",
        installation_id=device.installation_id,
        device_key_sha256=key_fingerprint(device.public_key),
        device_limit=2,
        revision=7,
        iat=int(clock.wall),
        exp=int(clock.wall) + 60,
        jti="unique-test-token",
        request_nonce=secrets.token_urlsafe(32),
    )
    gate = LocalLeaseGate(
        public_keys={"test-key": signer.public_key().public_bytes_raw()},
        issuer="test-issuer",
        audience="test-panel",
        installation_id=device.installation_id,
        device_key_sha256=key_fingerprint(device.public_key),
        wall_clock=lambda: clock.wall,
        monotonic_clock=lambda: clock.monotonic,
    )
    return GateCase(gate, clock, signer.private_bytes_raw(), payload)


def test_gate_starts_closed_and_monotonic_ttl_is_conservative(case):
    with pytest.raises(LicenseUnavailable):
        case.gate.require_valid()
    started = case.clock.monotonic
    case.clock.advance(5)
    assert case.accept(request_started_monotonic=started).device_limit == 2
    case.clock.advance(54)
    assert case.gate.require_valid().revision == 7
    case.clock.advance(1)
    with pytest.raises(LicenseUnavailable, match="expired"):
        case.gate.require_valid()


@pytest.mark.parametrize(
    "override",
    [
        {"iss": "other-service"},
        {"aud": "other-client"},
        {"installation_id": "other-device"},
        {"activation_id": "other-activation"},
        {"device_key_sha256": "wrong-key"},
        {"request_nonce": "old-request"},
        {"exp": 1893456061},
        {"exp": 1893456000},
        {"iat": True},
        {"device_limit": 0},
        {"revision": 0},
        {"v": 2},
        {"sub": ""},
    ],
)
def test_signed_but_invalid_scope_binding_and_claims_fail_closed(case, override):
    case.accept()
    with pytest.raises(LicenseUnavailable):
        case.accept(case.token(**override))
    with pytest.raises(LicenseUnavailable):
        case.gate.require_valid()


def test_bad_signature_and_unknown_key_fail_closed(case):
    forged = sign_lease(case.payload, Ed25519PrivateKey.generate().private_bytes_raw(), "test-key")
    with pytest.raises(LicenseUnavailable):
        case.accept(forged)
    with pytest.raises(LicenseUnavailable):
        case.accept(sign_lease(case.payload, case.key, "unknown-key"))


def test_replayed_response_cannot_refresh_deadline(case):
    token = case.token()
    case.accept(token)
    case.clock.advance(1)
    with pytest.raises(LicenseUnavailable):
        case.accept(token)
    with pytest.raises(LicenseUnavailable):
        case.accept(token, request_nonce="fresh-unrelated-request")


@pytest.mark.parametrize("wall_delta,mono_delta", [(-120, 30), (10, -1), (60, 1)])
def test_clock_rollback_and_expiry_never_extend_a_lease(case, wall_delta, mono_delta):
    case.accept()
    case.clock.wall += wall_delta
    case.clock.monotonic += mono_delta
    with pytest.raises(LicenseUnavailable):
        case.gate.require_valid()


def test_delayed_online_response_and_incorrect_clock_are_rejected(case):
    with pytest.raises(LicenseUnavailable):
        case.accept(request_started_monotonic=case.clock.monotonic - 16)
    case.clock.wall -= 40
    with pytest.raises(LicenseUnavailable):
        case.accept()


def test_long_renewal_loop_keeps_nonce_memory_bounded(case):
    for _ in range(2000):
        case.payload.update(
            iat=int(case.clock.wall),
            exp=int(case.clock.wall) + 60,
            request_nonce=secrets.token_urlsafe(32),
        )
        case.accept()
        case.clock.advance(30)
        assert case.gate.require_valid()
    assert len(case.gate._consumed_nonces) <= 1
