"""Independent CLI boundary checks; all files and identities are synthetic."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from fsm_panel import config_cli
from fsm_panel.adapters import account_config as configs

PRIVATE_LOGIN = "QA_PRIVATE_LOGIN_SENTINEL"
PRIVATE_SETTING = "QA_PRIVATE_SETTING_SENTINEL"
PRIVATE_CONTENT = "QA_PRIVATE_CONTENT_SENTINEL"


def tree(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def output(capsys):
    captured = capsys.readouterr()
    assert not captured.err
    for private in (PRIVATE_LOGIN, PRIVATE_SETTING, PRIVATE_CONTENT):
        assert private not in captured.out
    return json.loads(captured.out)


@pytest.fixture
def inputs(tmp_path, capsys):
    root = tmp_path / "owned"
    assert config_cli.main(["workspace", str(root)]) == 0
    assert output(capsys)["operation"] == "workspace_created"
    settings, video, machine, localcopy = (tmp_path / name for name in ("s.json", "v", "m", "lc"))
    settings.write_text(
        json.dumps(
            {
                "STEAM_PATH": r"Z:\logical-only\steam.exe",
                "DISABLE_STEAM_OVERLAY": True,
                "NO_AVAST_MODE": False,
                "TG_BOT_API_KEY": PRIVATE_SETTING,
            }
        ),
        encoding="utf-8",
    )
    video.write_bytes(b"synthetic video\n")
    machine.write_bytes(b"synthetic machine\n")
    localcopy.write_bytes((PRIVATE_CONTENT + "\n").encode())
    account = root / "steam" / "userdata" / "42"
    cfg = account / "730" / "local" / "cfg"
    cfg.mkdir(parents=True)
    (cfg / "cs2_video.txt").write_bytes(b"old video")
    (cfg / "cs2_machine_convars.vcfg").write_bytes(b"old machine")
    (account / "unrelated").write_bytes(b"preserve this member")
    game = root / "game" / "game" / "csgo" / "cfg"
    game.mkdir(parents=True)
    (game / "autoexec.cfg").write_bytes(b"synthetic video\r\n")
    (game / "custom.cfg").write_bytes(b"preserve legacy during no-op")
    command = [
        "account",
        "--workspace",
        str(root),
        "--settings",
        str(settings),
        "--steam-id",
        "76561197960265770",
        "--login",
        PRIVATE_LOGIN,
        "--video-source",
        str(video),
        "--machine-source",
        str(machine),
        "--localconfig-copy",
        str(localcopy),
        "--timestamp",
        "2000.5",
    ]
    autoexec = [
        "autoexec",
        "--workspace",
        str(root),
        "--source",
        str(video),
        "--config-file-name",
        "custom.cfg",
    ]
    return SimpleNamespace(
        root=root,
        settings=settings,
        video=video,
        machine=machine,
        localcopy=localcopy,
        account=account,
        cfg=cfg,
        game=game,
        command=command,
        autoexec=autoexec,
    )


def replace_arg(argv, option, value):
    command = list(argv)
    command[command.index(option) + 1] = str(value)
    return command


@pytest.mark.parametrize("changed", ["setting", "copy", "login", "force", "destination"])
def test_review_fingerprint_rejects_changed_effective_inputs(inputs, capsys, changed):
    assert config_cli.main(inputs.command) == 0
    first = output(capsys)
    assert len(first["fingerprint"]) == 64
    assert set(first["fingerprint"]) <= set("0123456789abcdef")
    command = list(inputs.command)
    if changed == "setting":
        settings = json.loads(inputs.settings.read_text())
        settings["NO_AVAST_MODE"] = True
        inputs.settings.write_text(json.dumps(settings))
    elif changed == "copy":
        inputs.localcopy.write_bytes(b"different mapped content\n")
    elif changed == "login":
        command = replace_arg(command, "--login", PRIVATE_LOGIN + "_changed")
    elif changed == "force":
        command.append("--force")
    else:
        (inputs.account / "unrelated").write_bytes(b"independent destination change")
    before = tree(inputs.root)
    assert config_cli.main(command + ["--apply", "--expect-fingerprint", first["fingerprint"]]) == 2
    assert output(capsys) == {"error": "config_preview_stale"}
    assert tree(inputs.root) == before


def test_token_binds_auxiliary_snapshot_without_printing_unused_settings_secrets(inputs, capsys):
    assert config_cli.main(inputs.command) == 0
    first = output(capsys)["fingerprint"]
    assert config_cli.main(inputs.command) == 0
    assert output(capsys)["fingerprint"] == first
    settings = json.loads(inputs.settings.read_text())
    settings["TG_BOT_API_KEY"] = PRIVATE_SETTING + "_CHANGED"
    inputs.settings.write_text(json.dumps(settings, indent=4))
    assert config_cli.main(inputs.command) == 0
    assert output(capsys)["fingerprint"] != first
    # Correlation includes the entire reviewed settings snapshot, even fields
    # unused by the selected operation. Only a digest may leave the CLI.


@pytest.mark.parametrize("which", ["settings", "localcopy"])
@pytest.mark.parametrize("change", ["metadata", "identity"])
def test_auxiliary_file_identity_changes_invalidate_same_text_proposal(
    inputs, capsys, which, change
):
    assert config_cli.main(inputs.command) == 0
    fingerprint = output(capsys)["fingerprint"]
    path = getattr(inputs, which)
    raw, details = path.read_bytes(), path.stat()
    if change == "metadata":
        os.utime(path, ns=(details.st_atime_ns, details.st_mtime_ns + 1_000_000_000))
    else:
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(raw)
        os.utime(replacement, ns=(details.st_atime_ns, details.st_mtime_ns))
        os.replace(replacement, path)
        assert path.stat().st_ino != details.st_ino
    assert path.read_bytes() == raw
    original = tree(inputs.root)
    assert config_cli.main(inputs.command + ["--apply", "--expect-fingerprint", fingerprint]) == 2
    assert output(capsys) == {"error": "config_preview_stale"}
    assert tree(inputs.root) == original


@pytest.mark.parametrize("which", ["settings", "localcopy"])
def test_auxiliary_input_changed_during_preview_prevents_apply(inputs, capsys, monkeypatch, which):
    native_preview = config_cli.preview_account_config

    def change_after_preview(*args, **kwargs):
        preview = native_preview(*args, **kwargs)
        path = getattr(inputs, which)
        path.write_bytes(path.read_bytes() + b" ")
        return preview

    monkeypatch.setattr(config_cli, "preview_account_config", change_after_preview)
    original = tree(inputs.account)
    assert config_cli.main(inputs.command + ["--apply"]) == 2
    assert output(capsys) == {"error": "config_preview_stale"}
    assert tree(inputs.account) == original
    assert not list(inputs.root.glob(".config-*"))


@pytest.mark.parametrize("which", ["settings", "localcopy"])
def test_auxiliary_input_changed_while_staging_prevents_commit(inputs, capsys, monkeypatch, which):
    native_copy = configs.shutil.copytree

    def edit_after_copy(source, destination, *args, **kwargs):
        result = native_copy(source, destination, *args, **kwargs)
        if Path(source) == inputs.account:
            path = getattr(inputs, which)
            path.write_bytes(path.read_bytes() + b" ")
        return result

    monkeypatch.setattr(configs.shutil, "copytree", edit_after_copy)
    original = tree(inputs.account)
    assert config_cli.main(inputs.command + ["--apply"]) == 2
    assert output(capsys) == {"error": "config_preview_stale"}
    assert tree(inputs.account) == original
    assert not list(inputs.root.glob(".config-*"))


def test_noop_cli_keeps_legacy_file_identity_and_requires_current_fingerprint(inputs, capsys):
    before = {
        path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
        for path in inputs.game.iterdir()
    }
    assert config_cli.main(inputs.autoexec) == 0
    preview = output(capsys)
    assert preview["changed"] is False
    assert (
        config_cli.main(
            inputs.autoexec + ["--apply", "--expect-fingerprint", preview["fingerprint"]]
        )
        == 0
    )
    applied = output(capsys)
    assert applied["changed"] is False and applied["backup_path"] is None
    assert {
        path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) for path in before
    } == before
    (inputs.game / "custom.cfg").write_bytes(b"independent legacy file change")
    assert (
        config_cli.main(
            inputs.autoexec + ["--apply", "--expect-fingerprint", preview["fingerprint"]]
        )
        == 2
    )
    assert output(capsys)["error"] == "config_preview_stale"
    assert not list(inputs.root.glob(".config-*"))


@pytest.mark.parametrize("rollback_denied", [False, True])
def test_cli_failed_commit_reports_exact_recovery_paths_without_success(
    inputs, capsys, monkeypatch, rollback_denied
):
    original = tree(inputs.account)
    native_replace = configs._replace_bounded

    def fail_install(source, destination, workspace):
        if source.name.startswith(".config-stage-"):
            raise OSError(PRIVATE_CONTENT)
        if source.name.startswith(".config-backup-") and rollback_denied:
            raise OSError(PRIVATE_SETTING)
        return native_replace(source, destination, workspace)

    monkeypatch.setattr(configs, "_replace_bounded", fail_install)
    assert config_cli.main(inputs.command + ["--apply"]) == 2
    result = output(capsys)
    assert "operation" not in result and "changed" not in result
    assert result["rollback_succeeded"] is (not rollback_denied)
    assert result["staging_path"] is None
    if rollback_denied:
        backup = Path(result["backup_path"])
        assert backup.is_relative_to(inputs.root) and tree(backup) == original
        assert not inputs.account.exists()
    else:
        assert result["backup_path"] is None and tree(inputs.account) == original


@pytest.mark.parametrize(
    "body",
    [b"[]", b'{"STEAM_PATH":false}', b'{"private":' + b"[" * 1200 + b"0" + b"]" * 1200 + b"}"],
)
def test_malformed_settings_produce_only_a_content_free_error(inputs, capsys, body):
    inputs.settings.write_bytes(body)
    original = tree(inputs.root)
    assert config_cli.main(inputs.command + ["--apply"]) == 2
    assert output(capsys) == {"error": "config_invalid_settings"}
    assert tree(inputs.root) == original


def test_crlf_localconfig_copy_is_not_double_translated(inputs, capsys):
    original = (PRIVATE_CONTENT + "\r\nsecond line\r\n").encode()
    inputs.localcopy.write_bytes(original)
    assert config_cli.main(inputs.command + ["--apply"]) == 0
    output(capsys)
    assert (inputs.account / "config" / "localconfig.vdf").read_bytes() == original
    assert inputs.localcopy.read_bytes() == original


@pytest.mark.parametrize("which", ["settings", "localcopy"])
def test_auxiliary_sources_inside_output_tree_cannot_be_overwritten(inputs, capsys, which):
    if which == "settings":
        path, option = inputs.cfg / "cs2_video.txt", "--settings"
    else:
        path, option = inputs.cfg / "cs2_machine_convars.vcfg", "--localconfig-copy"
    path.write_bytes(getattr(inputs, which).read_bytes())
    original = tree(inputs.root)
    command = replace_arg(inputs.command, option, path)
    assert config_cli.main(command + ["--apply"]) == 2
    assert "error" in output(capsys)
    assert tree(inputs.root) == original


def test_deep_marker_json_is_a_structured_error_at_module_entry(inputs):
    marker = inputs.root / ".fsm-config-workspace.json"
    marker.write_bytes(b' {"token":' + b"[" * 1200 + b"0" + b"]" * 1200 + b"}")
    original = tree(inputs.root)
    result = subprocess.run(
        [sys.executable, "-m", "fsm_panel.config_cli", *inputs.autoexec, "--apply"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2, "malformed marker must not escape as a traceback"
    assert not result.stderr
    assert "error" in json.loads(result.stdout)
    assert tree(inputs.root) == original


def test_invalid_numeric_argument_is_not_echoed_into_error_output(inputs):
    command = replace_arg(inputs.command, "--timestamp", PRIVATE_CONTENT)
    result = subprocess.run(
        [sys.executable, "-m", "fsm_panel.config_cli", *command, "--apply"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert PRIVATE_CONTENT not in result.stdout + result.stderr
    assert not result.stderr
    assert "error" in json.loads(result.stdout)


def test_module_help_remains_available_without_opening_a_workspace():
    result = subprocess.run(
        [sys.executable, "-m", "fsm_panel.config_cli", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0 and not result.stderr
    assert "usage:" in result.stdout and "workspace" in result.stdout


def test_success_reports_usable_backup_without_printing_input_contents(inputs, capsys):
    original = tree(inputs.account)
    assert config_cli.main(inputs.command + ["--apply"]) == 0
    result = output(capsys)
    assert result["operation"] == "apply" and result["changed"]
    assert tree(Path(result["backup_path"])) == original
    assert (inputs.account / "config" / "localconfig.vdf").read_text() == PRIVATE_CONTENT + "\n"
    assert (inputs.account / "unrelated").read_bytes() == b"preserve this member"
