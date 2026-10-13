import json
from pathlib import Path

import pytest

from fsm_panel.config_cli import main


@pytest.fixture
def inputs(tmp_path, capsys):
    workspace = tmp_path / "isolated"
    assert main(["workspace", str(workspace)]) == 0
    capsys.readouterr()
    settings, video, machine = (tmp_path / name for name in ("settings.json", "v.txt", "m.txt"))
    settings.write_text(
        json.dumps(
            {
                "STEAM_PATH": "C:\\unused-real-path\\steam.exe",
                "DISABLE_STEAM_OVERLAY": True,
                "NO_AVAST_MODE": False,
            }
        )
    )
    video.write_text("synthetic video")
    machine.write_text("synthetic machine")
    command = [
        "account",
        "--workspace",
        str(workspace),
        "--settings",
        str(settings),
        "--steam-id",
        "76561197960265770",
        "--login",
        "synthetic_private_login",
        "--video-source",
        str(video),
        "--machine-source",
        str(machine),
        "--timestamp",
        "2000.5",
    ]
    return workspace, settings, video, machine, command


def result(capsys):
    raw = capsys.readouterr().out
    assert "synthetic_private_login" not in raw
    assert "synthetic video" not in raw
    return json.loads(raw)


def test_account_review_fingerprint_apply_and_retained_backup(inputs, capsys):
    workspace, _, video, machine, command = inputs
    target = workspace / "steam/userdata/42"
    assert main(command) == 0
    first = result(capsys)
    assert first["operation"] == "preview" and not target.exists()
    assert main(command) == 0
    assert result(capsys)["fingerprint"] == first["fingerprint"]
    assert main(command + ["--apply", "--expect-fingerprint", first["fingerprint"]]) == 0
    assert result(capsys)["backup_path"] is None
    assert (target / "730/local/cfg/cs2_video.txt").read_text() == "synthetic video"
    assert (target / "730/local/cfg/cs2_machine_convars.vcfg").read_text() == "synthetic machine"
    video.write_text("new video")
    assert main(command + ["--apply"]) == 0
    backup = Path(result(capsys)["backup_path"])
    assert backup.is_dir()
    assert (backup / "730/local/cfg/cs2_video.txt").read_text() == "synthetic video"
    assert machine.read_text() == "synthetic machine"


def test_stale_expected_fingerprint_blocks_new_preview_even_before_first_apply(inputs, capsys):
    workspace, _, video, _, command = inputs
    assert main(command) == 0
    fingerprint = result(capsys)["fingerprint"]
    video.write_text("changed between commands")
    assert main(command + ["--apply", "--expect-fingerprint", fingerprint]) == 2
    assert result(capsys)["error"] == "config_preview_stale"
    assert not (workspace / "steam/userdata/42").exists()


def test_workspace_creation_never_adopts_existing_directory(inputs, capsys):
    workspace, *_ = inputs
    marker = (workspace / ".fsm-config-workspace.json").read_bytes()
    assert main(["workspace", str(workspace)]) == 2
    assert "error" in result(capsys)
    assert (workspace / ".fsm-config-workspace.json").read_bytes() == marker


def test_autoexec_equal_preserves_old_named_file_and_mismatch_backs_it_up(inputs, capsys):
    workspace, _, source, _, _ = inputs
    cfg = workspace / "game/game/csgo/cfg"
    cfg.mkdir(parents=True)
    autoexec, legacy = cfg / "autoexec.cfg", cfg / "explicit.cfg"
    source.write_text("first\n")
    autoexec.write_text("first\n")
    legacy.write_text("retain")
    command = [
        "autoexec",
        "--workspace",
        str(workspace),
        "--source",
        str(source),
        "--config-file-name",
        "explicit.cfg",
        "--apply",
    ]
    before = autoexec.stat().st_mtime_ns
    assert main(command) == 0
    assert result(capsys)["changed"] is False
    assert legacy.read_text() == "retain" and autoexec.stat().st_mtime_ns == before
    source.write_text("second\n")
    assert main(command) == 0
    backup = Path(result(capsys)["backup_path"])
    assert (backup / "explicit.cfg").read_text() == "retain"
    assert not legacy.exists() and autoexec.read_text() == "second\n"


def test_invalid_settings_are_content_free_and_change_no_account(inputs, capsys):
    workspace, settings, _, _, command = inputs
    settings.write_text('{"private_secret":"never_display",')
    assert main(command + ["--apply"]) == 2
    raw = capsys.readouterr().out
    assert "never_display" not in raw and "error" in json.loads(raw)
    assert not (workspace / "steam/userdata/42").exists()
