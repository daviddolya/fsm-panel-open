import os

import pytest

from fsm_panel.adapters.secrets import DPAPIProtector, DPAPISecretStore
from fsm_panel.core import UnavailableError, ValidationError


class TestOnlyProtector:
    """Portable test seam, intentionally not a production cryptosystem."""

    __test__ = False

    def protect(self, value):
        return b"test-ciphertext:" + bytes(byte ^ 0xA5 for byte in value)

    def unprotect(self, value):
        if not value.startswith(b"test-ciphertext:"):
            raise ValueError("synthetic damaged ciphertext")
        return bytes(byte ^ 0xA5 for byte in value[len(b"test-ciphertext:") :])


def test_portable_protector_seam_and_no_plaintext_files(tmp_path):
    store = DPAPISecretStore(tmp_path, protector=TestOnlyProtector())
    value = b"synthetic_password_do_not_log"
    store.put("synthetic-reference", value)
    assert store.get("synthetic-reference") == value
    assert all(value not in path.read_bytes() for path in tmp_path.iterdir())
    assert "synthetic_password" not in repr(store)
    store.delete("synthetic-reference")
    store.delete("synthetic-reference")
    with pytest.raises(UnavailableError, match="secret_read_failed"):
        store.get("synthetic-reference")


@pytest.mark.parametrize("reference", ["../escape", "C:\\escape", "bad/name", "", "a" * 129])
def test_secret_references_cannot_escape_store(tmp_path, reference):
    store = DPAPISecretStore(tmp_path, protector=TestOnlyProtector())
    with pytest.raises(ValidationError, match="invalid_secret_reference"):
        store.put(reference, b"synthetic")
    assert list(tmp_path.iterdir()) == []


def test_corrupt_ciphertext_has_safe_error(tmp_path):
    store = DPAPISecretStore(tmp_path, protector=TestOnlyProtector())
    (tmp_path / "synthetic.dpapi").write_bytes(b"synthetic_plaintext")
    with pytest.raises(UnavailableError) as error:
        store.get("synthetic")
    assert "synthetic_plaintext" not in str(error.value)


@pytest.mark.skipif(os.name != "nt", reason="Windows current-user DPAPI contract")
def test_windows_dpapi_roundtrip_only_uses_synthetic_secret(tmp_path):
    store = DPAPISecretStore(tmp_path)
    value = b"synthetic-test-only-secret-123"
    store.put("synthetic", value)
    assert value not in (tmp_path / "synthetic.dpapi").read_bytes()
    assert store.get("synthetic") == value


def test_no_plaintext_protection_fallback(tmp_path):
    class BrokenProtector(TestOnlyProtector):
        def protect(self, value):
            return value

    store = DPAPISecretStore(tmp_path, protector=BrokenProtector())
    with pytest.raises(ValidationError, match="secret_protection_invalid"):
        store.put("synthetic", b"synthetic_secret")
    assert not list(tmp_path.iterdir())


def test_non_windows_dpapi_is_explicitly_unavailable(monkeypatch):
    monkeypatch.setattr("fsm_panel.adapters.secrets.os.name", "posix")
    with pytest.raises(UnavailableError, match="dpapi_requires_windows"):
        DPAPIProtector()
