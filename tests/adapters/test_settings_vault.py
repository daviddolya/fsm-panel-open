import json

import pytest

from fsm_panel.adapters.fakes import MemorySecretStore
from fsm_panel.adapters.settings_vault import SettingsVault
from fsm_panel.core import SettingsDocument, SettingsStore, ValidationError


def test_sensitive_settings_roundtrip_without_plaintext_in_files(tmp_path):
    secrets = MemorySecretStore()
    vault = SettingsVault(secrets)
    values = SettingsStore.defaults().to_dict(include_secrets=True)
    values.update(
        {
            "TG_BOT_API_KEY": "synthetic_bot_token_never_write",
            "TRADEOFFER_LINK": "synthetic_trade_secret_never_write",
            "future": [
                {"password": "synthetic_nested_password", "ordinary": 123},
                {"api_key": {"nested": [True, "synthetic_sensitive_object"]}},
            ],
        }
    )
    document = SettingsDocument(values)
    path = tmp_path / "settings.vault.json"
    vault.save(document, path)
    raw = path.read_text()
    assert "synthetic_" not in raw
    assert vault.load(path).to_dict(include_secrets=True) == values
    assert "synthetic_" not in repr(vault)


def test_backup_references_remain_valid_after_new_save(tmp_path):
    vault = SettingsVault(MemorySecretStore())
    path = tmp_path / "settings.vault.json"
    vault.save(SettingsDocument({"TG_BOT_API_KEY": "synthetic_old"}), path)
    vault.save(SettingsDocument({"TG_BOT_API_KEY": "synthetic_new"}), path)
    backup = path.with_suffix(".json.bak")
    assert vault.load(backup).get("TG_BOT_API_KEY") == "synthetic_old"
    assert vault.load(path).get("TG_BOT_API_KEY") == "synthetic_new"
    assert vault.restore_backup(path).get("TG_BOT_API_KEY") == "synthetic_old"
    assert "synthetic_old" not in path.read_text() + backup.read_text()


def test_failed_replace_preserves_prior_readable_settings(tmp_path, monkeypatch):
    vault = SettingsVault(MemorySecretStore())
    path = tmp_path / "settings.vault.json"
    vault.save(SettingsDocument({"TG_BOT_API_KEY": "synthetic_old"}), path)
    original = path.read_bytes()

    def fail(*args):
        raise OSError("synthetic failure")

    monkeypatch.setattr("fsm_panel.adapters.settings_vault.atomic_write", fail)
    with pytest.raises(ValidationError, match="settings_vault_save_failed"):
        vault.save(SettingsDocument({"TG_BOT_API_KEY": "synthetic_new"}), path)
    assert path.read_bytes() == original
    assert vault.load(path).get("TG_BOT_API_KEY") == "synthetic_old"


def test_failed_protection_never_replaces_existing_settings(tmp_path):
    class FailingSecrets(MemorySecretStore):
        fail = False

        def put(self, reference, value):
            if self.fail:
                raise RuntimeError("synthetic_secret_do_not_log")
            super().put(reference, value)

    secrets = FailingSecrets()
    vault = SettingsVault(secrets)
    path = tmp_path / "settings.vault.json"
    vault.save(SettingsDocument({"TG_BOT_API_KEY": "synthetic_old"}), path)
    original = path.read_bytes()
    secrets.fail = True
    with pytest.raises(ValidationError) as error:
        vault.save(SettingsDocument({"TG_BOT_API_KEY": "synthetic_new"}), path)
    assert "synthetic_secret" not in str(error.value)
    assert path.read_bytes() == original


def test_plaintext_legacy_file_is_not_silently_backed_up_as_vault(tmp_path):
    path = tmp_path / "legacy.json"
    original = '{"TG_BOT_API_KEY":"synthetic_old"}'
    path.write_text(original)
    vault = SettingsVault(MemorySecretStore())
    with pytest.raises(ValidationError):
        vault.save(SettingsDocument({"TG_BOT_API_KEY": "synthetic_new"}), path)
    assert path.read_text() == original
    assert not path.with_suffix(".json.bak").exists()


@pytest.mark.parametrize("tamper", ["plaintext", "duplicate", "missing", "path", "reference"])
def test_tampered_vault_fails_closed(tmp_path, tamper):
    vault = SettingsVault(MemorySecretStore())
    path = tmp_path / "settings.vault.json"
    vault.save(SettingsDocument({"TG_BOT_API_KEY": "synthetic_original"}), path)
    envelope = json.loads(path.read_text())
    if tamper == "plaintext":
        envelope["values"]["TG_BOT_API_KEY"] = "synthetic_plaintext"
    elif tamper == "duplicate":
        envelope["secrets"].append(envelope["secrets"][0])
    elif tamper == "missing":
        envelope["secrets"] = []
    elif tamper == "path":
        envelope["secrets"][0]["path"] = ["ordinary"]
    else:
        envelope["secrets"][0]["reference"] = "settings-missing"
    path.write_text(json.dumps(envelope))
    with pytest.raises(ValidationError, match="settings_vault_load_failed"):
        vault.load(path)
