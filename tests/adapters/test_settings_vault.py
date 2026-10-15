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


@pytest.mark.parametrize(
    "key", ["PROXY_FOR_TG", "proxy_for_tg", "tg_bot_chat_id", "tradeoffer_link"]
)
def test_v1_new_sensitive_fields_load_readonly_then_upgrade_with_sealed_backup(tmp_path, key):
    vault = SettingsVault(MemorySecretStore())
    path = tmp_path / "old.vault.json"
    old = {
        "format": "fsm-panel-settings-v1",
        "values": {key: "synthetic_old_private", "ordinary": 1},
        "secrets": [],
    }
    path.write_text(json.dumps(old))
    before = path.read_bytes()
    document = vault.load(path)
    assert path.read_bytes() == before
    assert document.get(key) == "synthetic_old_private"
    assert document.to_dict()[key] == "[redacted]"
    document.set("ordinary", 2)
    vault.save(document, path)
    backup = path.with_suffix(".json.bak")
    for candidate in (path, backup):
        raw = candidate.read_text()
        assert "synthetic_old_private" not in raw
        assert json.loads(raw)["format"] == "fsm-panel-settings-v2"
        assert vault.load(candidate).get(key) == "synthetic_old_private"
    assert vault.load(backup).get("ordinary") == 1
    assert vault.load(path).get("ordinary") == 2


@pytest.mark.parametrize(
    "version,key",
    [
        ("v1", "TG_BOT_API_KEY"),
        ("v1", "TRADEOFFER_LINK"),
        ("v1", "nested_password"),
        ("v2", "PROXY_FOR_TG"),
    ],
)
def test_version_policy_rejects_unprotected_previously_sensitive_fields(tmp_path, version, key):
    path = tmp_path / "tampered.json"
    path.write_text(
        json.dumps(
            {
                "format": f"fsm-panel-settings-{version}",
                "values": {key: "synthetic_private"},
                "secrets": [],
            }
        )
    )
    with pytest.raises(ValidationError, match="settings_vault_load_failed"):
        SettingsVault(MemorySecretStore()).load(path)


def test_failed_v1_upgrade_retains_original_and_readable_sealed_backup(tmp_path, monkeypatch):
    from fsm_panel.adapters import settings_vault as module

    path = tmp_path / "old.json"
    old = {
        "format": "fsm-panel-settings-v1",
        "values": {"PROXY_FOR_TG": "synthetic_private"},
        "secrets": [],
    }
    path.write_text(json.dumps(old))
    before = path.read_bytes()
    vault = SettingsVault(MemorySecretStore())
    real_write = module.atomic_write

    def fail_target(target, raw):
        if target == path:
            raise OSError("synthetic_write_failure")
        return real_write(target, raw)

    monkeypatch.setattr(module, "atomic_write", fail_target)
    with pytest.raises(ValidationError):
        vault.save(vault.load(path), path)
    assert path.read_bytes() == before
    backup = path.with_suffix(".json.bak")
    assert "synthetic_private" not in backup.read_text()
    assert vault.load(backup).get("PROXY_FOR_TG") == "synthetic_private"


@pytest.mark.parametrize("protected", [False, True])
def test_v1_new_sensitive_container_retains_old_nested_reference_checks(tmp_path, protected):
    secrets = MemorySecretStore()
    path = tmp_path / "nested.json"
    envelope = {
        "format": "fsm-panel-settings-v1",
        "values": {"PROXY_FOR_TG": {"PASSWORD": None if protected else "synthetic_nested"}},
        "secrets": [],
    }
    if protected:
        secrets.put("settings-nested", b'"synthetic_nested"')
        envelope["secrets"].append(
            {"path": ["PROXY_FOR_TG", "PASSWORD"], "reference": "settings-nested"}
        )
    path.write_text(json.dumps(envelope))
    vault = SettingsVault(secrets)
    if not protected:
        with pytest.raises(ValidationError):
            vault.load(path)
    else:
        document = vault.load(path)
        assert document.get("PROXY_FOR_TG") == {"PASSWORD": "synthetic_nested"}
        vault.save(document, path)
        assert "synthetic_nested" not in path.read_text()
        assert vault.load(path).get("PROXY_FOR_TG") == {"PASSWORD": "synthetic_nested"}
