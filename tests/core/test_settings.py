import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fsm_panel.core import SETTINGS_TYPES, SettingsDocument, SettingsStore, ValidationError

json_scalars = st.none() | st.booleans() | st.integers() | st.floats(
    allow_nan=False, allow_infinity=False
) | st.text(alphabet=st.characters(blacklist_categories=("Cs",)))
json_values = st.recursive(
    json_scalars,
    lambda children: st.lists(children, max_size=5) | st.dictionaries(
        st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=15),
        children, max_size=5,
    ), max_leaves=15,
)


def test_all_49_observed_keys_roundtrip_with_unknowns_and_exact_types(tmp_path):
    assert len(SETTINGS_TYPES) == 49
    values = SettingsStore.defaults().to_dict(include_secrets=True)
    values["TG_BOT_API_KEY"] = "synthetic_token_do_not_log"
    values["STEAM_PATH"] = "C:\\Тестовый путь\\steam.exe"
    values["future"] = {"mixed": [True, 1, 1.0, None, "кириллица"]}
    document = SettingsDocument(values)
    path = tmp_path / "settings.json"
    document.save(path)
    loaded = SettingsStore.load(path)
    assert loaded.to_dict(include_secrets=True) == values
    assert all(type(loaded.get(key)) is expected for key, expected in SETTINGS_TYPES.items())
    assert "synthetic_token" not in repr(document)
    assert loaded.redacted()["TG_BOT_API_KEY"] == "[redacted]"
    assert loaded.get("AUTO_LOOT") is False
    assert loaded.get("START_FARM_WHEN_LAUNCHED") is False


@given(json_values)
@settings(max_examples=50)
def test_unknown_json_values_roundtrip_without_coercion(value):
    document = SettingsDocument({"future_extension": value})
    encoded = json.dumps(document.to_dict(include_secrets=True), allow_nan=False)
    decoded = SettingsStore.loads(encoded).get("future_extension")
    assert decoded == value
    assert type(decoded) is type(value)


@pytest.mark.parametrize("content", [
    "{", "[]", '{"ANTI_AFK":1}', '{"ACCOUNT_LOGIN_ATTEMPTS":true}',
    '{"STATS_SKIN_PRICE":1}', '{"future": NaN}', '{"x":1,"x":2}',
    '{"x":{"same":1,"same":2}}', b"\xff", '{"STEAM_PATH":null}',
])
def test_corrupt_or_type_changed_json_rejected(content):
    with pytest.raises(ValidationError):
        SettingsStore.loads(content)


def test_backup_is_previous_exact_content_and_can_restore(tmp_path):
    path = tmp_path / "settings.json"
    original = b'{"ANTI_AFK": false, "future": [1,true]}\n'
    path.write_bytes(original)
    document = SettingsStore.load(path)
    document.set("ANTI_AFK", True)
    document.save(path)
    assert path.with_suffix(".json.bak").read_bytes() == original
    assert SettingsStore.restore_backup(path).get("ANTI_AFK") is False


def test_failed_replace_preserves_original_and_removes_temporary(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    SettingsDocument({"ANTI_AFK": False}).save(path)
    original = path.read_bytes()

    def fail(*args):
        raise OSError("synthetic disk fault")

    monkeypatch.setattr("fsm_panel.core.settings.os.replace", fail)
    with pytest.raises(ValidationError, match="settings_save_failed"):
        SettingsDocument({"ANTI_AFK": True}).save(path, backup=False)
    assert path.read_bytes() == original
    assert len(list(tmp_path.iterdir())) == 1


def test_redaction_is_recursive_and_get_returns_copy():
    doc = SettingsDocument({"future": {"password": "synthetic", "items": [1]},
                            "TG_BOT_CHAT_ID": "synthetic_private_id"})
    assert doc.redacted()["future"]["password"] == "[redacted]"
    doc.get("future")["items"].append(2)
    assert doc.get("future")["items"] == [1]
    with pytest.raises(ValidationError):
        doc.set("ANTI_AFK", 0)


def test_missing_large_and_deep_settings_fail_safely(tmp_path):
    with pytest.raises(ValidationError, match="settings_read_failed"):
        SettingsStore.load(tmp_path / "missing.json")
    with pytest.raises(ValidationError):
        SettingsStore.loads(" " * (2 * 1024 * 1024 + 1))
    nested = {"x": None}
    for _ in range(45):
        nested = {"x": nested}
    with pytest.raises(ValidationError):
        SettingsDocument(nested)
