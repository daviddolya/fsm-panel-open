import os
import secrets
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fsm_license.config import Settings
from fsm_license.crypto import b64encode
from fsm_panel.licensing import DeviceIdentity, WindowsDpapiDeviceStore
from fsm_panel.licensing.device import DeviceStoreError


@pytest.mark.skipif(os.name != "nt", reason="requires actual Windows user-scope DPAPI")
def test_windows_dpapi_roundtrip_and_corrupt_store_preserves_evidence(tmp_path):
    identity = DeviceIdentity.generate()
    identity = replace(identity, activation_id=DeviceIdentity.generate().installation_id)
    store = WindowsDpapiDeviceStore(tmp_path / "device.bin")
    assert store.load() is None
    store.save(identity)
    assert store.load() == identity
    stored = store.path.read_bytes()
    assert identity.private_key not in stored
    assert b64encode(identity.private_key).encode() not in stored
    assert identity.installation_id.encode() not in stored
    store.path.write_bytes(b"deliberately corrupted encrypted test data")
    with pytest.raises(DeviceStoreError, match="preserve"):
        store.load()
    assert store.path.read_bytes() == b"deliberately corrupted encrypted test data"


def test_production_configuration_has_no_fallback_secrets(monkeypatch):
    for name in (
        "FSM_LICENSE_SIGNING_KEY",
        "FSM_LICENSE_DATABASE_URL",
        "FSM_LICENSE_INTERNAL_TOKEN",
        "FSM_LICENSE_ISSUER",
        "FSM_LICENSE_AUDIENCE",
        "FSM_LICENSE_KEY_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="explicitly"):
        Settings.from_env()
    signer = Ed25519PrivateKey.generate()
    token = secrets.token_urlsafe(48)
    values = {
        "FSM_LICENSE_DATABASE_URL": "postgresql+psycopg://localhost/test_only",
        "FSM_LICENSE_SIGNING_KEY": b64encode(signer.private_bytes_raw()),
        "FSM_LICENSE_INTERNAL_TOKEN": token,
        "FSM_LICENSE_ISSUER": "test-issuer",
        "FSM_LICENSE_AUDIENCE": "test-audience",
        "FSM_LICENSE_KEY_ID": "test-key",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    config = Settings.from_env()
    assert config.internal_token == token
    assert token not in repr(config) and repr(signer.private_bytes_raw()) not in repr(config)
    monkeypatch.setenv("FSM_LICENSE_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    with pytest.raises(ValueError, match="production requires"):
        Settings.from_env()


@pytest.mark.parametrize("seconds", [0, 61])
def test_lease_configuration_never_exceeds_60_seconds(seconds):
    with pytest.raises(ValueError, match="lease duration"):
        Settings(
            database_url="sqlite+pysqlite:///:memory:",
            internal_token=secrets.token_urlsafe(48),
            signing_key=Ed25519PrivateKey.generate().private_bytes_raw(),
            lease_seconds=seconds,
        )
