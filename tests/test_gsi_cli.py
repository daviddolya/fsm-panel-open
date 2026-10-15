import json
from pathlib import Path

import pytest

from fsm_panel.adapters.account_config import create_config_workspace
from fsm_panel.config_cli import main


@pytest.fixture
def inputs(tmp_path):
    workspace = create_config_workspace(tmp_path / "copy")
    cfg = workspace.game_root / "game/csgo/cfg"
    cfg.mkdir(parents=True)
    (cfg / "unrelated.cfg").write_bytes(b"preserved")
    token = tmp_path / "private-token.txt"
    token.write_bytes(b"synthetic_token_" + b"A" * 32 + b"\r\n")
    command = [
        "gsi",
        "--workspace",
        str(workspace.root),
        "--token-source",
        str(token),
        "--port",
        "6969",
        "--index",
        "1",
    ]
    return command, cfg, token


def test_gsi_preview_apply_rewrite_and_console_privacy(inputs, capsys):
    command, cfg, token = inputs
    assert main(command) == 0
    raw = capsys.readouterr().out
    assert "synthetic_token_" not in raw and "AAA" not in raw
    preview = json.loads(raw)
    assert preview["changed"] and preview["writes"] == 1
    assert not (cfg / "gamestate_integration_1.cfg").exists()
    assert main([*command, "--apply", "--expect-fingerprint", preview["fingerprint"]]) == 0
    applied = json.loads(capsys.readouterr().out)
    backup = Path(applied["backup_path"])
    assert (backup / "unrelated.cfg").read_bytes() == b"preserved"
    expected = (cfg / "gamestate_integration_1.cfg").read_bytes()
    assert b'"uri" "http://127.0.0.1:6969"' in expected
    assert token.read_bytes().rstrip(b"\r\n") in expected
    assert main(command) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["changed"]
    assert main([*command, "--apply", "--expect-fingerprint", again["fingerprint"]]) == 0
    rewritten = json.loads(capsys.readouterr().out)
    assert Path(rewritten["backup_path"]) != backup
    assert (cfg / "gamestate_integration_1.cfg").read_bytes() == expected


def test_changed_token_rejects_reviewed_digest_without_writing(inputs, capsys):
    command, cfg, token = inputs
    assert main(command) == 0
    fingerprint = json.loads(capsys.readouterr().out)["fingerprint"]
    token.write_text("synthetic_new_" + "B" * 32)
    assert main([*command, "--apply", "--expect-fingerprint", fingerprint]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "config_preview_stale"
    assert set(path.name for path in cfg.iterdir()) == {"unrelated.cfg"}


@pytest.mark.parametrize(
    ("flag", "value", "code"),
    [
        ("--port", "invalid_secret", "config_invalid_arguments"),
        ("--port", "0", "gsi_port_invalid"),
        ("--index", "11", "gsi_index_invalid"),
    ],
)
def test_bad_gsi_arguments_are_private_and_do_not_write(inputs, capsys, flag, value, code):
    command, cfg, _ = inputs
    command[command.index(flag) + 1] = value
    assert main(command) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["error"] == code
    assert "invalid_secret" not in output.out + output.err
    assert set(path.name for path in cfg.iterdir()) == {"unrelated.cfg"}
