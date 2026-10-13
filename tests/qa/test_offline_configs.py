"""Independent config/autoexec checks using only synthetic isolated file trees."""

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from fsm_panel.adapters import account_config as configs
from fsm_panel.adapters.account_config import (
    AccountConfigFacts,
    AccountConfigSettings,
    ConfigApplyError,
    ConfigError,
    ConfigStalePreviewError,
    apply_account_config,
    build_account_config_plan,
    create_config_workspace,
    load_config_templates,
    preview_account_config,
)
from fsm_panel.adapters.autoexec import apply_autoexec, preview_autoexec

STEAM_ID = 76561197960265770  # Synthetic account-directory result: 42.


def write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def contents(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def file_state(path):
    details = path.stat()
    return path.read_bytes(), details.st_ino, details.st_mtime_ns, details.st_mode


@pytest.fixture
def files(tmp_path):
    workspace = create_config_workspace(tmp_path / "owned-copy")
    video = write(tmp_path / "input" / "video.txt", b"synthetic video\r\nline two\n")
    machine = write(tmp_path / "input" / "machine.vcfg", b"synthetic machine\n")
    account = workspace.steam_root / "userdata" / "42"
    cfg = account / "730" / "local" / "cfg"
    write(cfg / "cs2_video.txt", b"previous video")
    write(cfg / "cs2_machine_convars.vcfg", b"previous machine")
    write(cfg / "cs2_user_convars_0_slot0.vcfg", b"remove convars")
    write(cfg / "cs2_user_convars_0_slot0.vcfg_lastclouded", b"remove clouded")
    write(cfg / "unrelated.bin", bytes(range(256)))
    write(account / "730" / "remote" / "nested" / "obsolete.bin", b"remove remote")
    write(account / "7" / "remote" / "unrelated.vdf", b"keep app7 sibling")
    localconfig = write(account / "config" / "localconfig.vdf", b"preserve localconfig")
    other_account = write(workspace.steam_root / "userdata" / "43" / "sentinel", b"other")
    source = write(tmp_path / "input" / "custom.cfg", b"synthetic autoexec\n")
    game_cfg = workspace.game_root / "game" / "csgo" / "cfg"
    autoexec = write(game_cfg / "autoexec.cfg", b"old autoexec\n")
    legacy = write(game_cfg / "custom.cfg", b"legacy")
    write(game_cfg / "nested" / "unrelated.cfg", b"keep nested")
    settings = AccountConfigSettings(
        r"Z:\synthetic-unrelated-path\steam.exe",
        "synthetic login",
        force=False,
        disable_steam_overlay=True,
        no_avast_mode=True,
        mapped_localconfig=None,
    )
    return SimpleNamespace(
        workspace=workspace,
        settings=settings,
        video=video,
        machine=machine,
        account=account,
        cfg=cfg,
        localconfig=localconfig,
        other_account=other_account,
        source=source,
        game_cfg=game_cfg,
        autoexec=autoexec,
        legacy=legacy,
    )


def account_preview(files, *, force=False):
    return preview_account_config(
        files.workspace,
        replace(files.settings, force=force),
        steam_id=STEAM_ID,
        video_source=files.video,
        machine_source=files.machine,
        now=120.5,
    )


def autoexec_preview(files):
    return preview_autoexec(
        files.workspace, source_path=files.source, config_file_name="custom.cfg"
    )


@pytest.mark.parametrize("force", [False, True])
def test_force_changes_logs_without_changing_copy_or_removal_scope(files, force):
    before = contents(files.account)
    untouched = {p: file_state(p) for p in (files.video, files.machine, files.other_account)}
    game_before = contents(files.game_cfg)
    preview = account_preview(files, force=force)
    assert preview.plan.account_id == "42"
    assert preview.plan.timestamp == 60  # Ties-to-even: round(120.5 - 60).
    assert bool(preview.plan.log_messages) is (not force)
    result = apply_account_config(preview)
    assert result.changed and result.target_path == files.account
    assert result.backup_path is not None and contents(result.backup_path) == before
    assert not (files.account / "730" / "remote").exists()
    assert not (files.cfg / "cs2_user_convars_0_slot0.vcfg").exists()
    assert not (files.cfg / "cs2_user_convars_0_slot0.vcfg_lastclouded").exists()
    assert (files.cfg / "cs2_video.txt").read_bytes() == b"synthetic video\r\nline two\r\n"
    assert (files.cfg / "cs2_machine_convars.vcfg").read_bytes() == b"synthetic machine\r\n"
    assert (files.cfg / "unrelated.bin").read_bytes() == bytes(range(256))
    assert files.localconfig.read_bytes() == b"preserve localconfig"
    assert (files.account / "7" / "remote" / "unrelated.vdf").read_bytes() == b"keep app7 sibling"
    assert contents(files.game_cfg) == game_before
    assert {p: file_state(p) for p in untouched} == untouched


@pytest.mark.parametrize(
    ("mapped", "no_avast", "exists", "expected", "last_content"),
    [
        (None, False, True, "untouched", None),
        ("", False, True, "restore", ""),
        (False, False, True, "restore", False),
        ("mapped", True, True, "preserve", None),
        ("", True, False, "create", "template"),
    ],
)
def test_localconfig_predicates_distinguish_none_from_false_and_empty(
    files, mapped, no_avast, exists, expected, last_content
):
    templates = load_config_templates()
    plan = build_account_config_plan(
        replace(
            files.settings,
            disable_steam_overlay=False,
            no_avast_mode=no_avast,
            mapped_localconfig=mapped,
        ),
        steam_id=STEAM_ID,
        video="video",
        machine="machine",
        templates=templates,
        facts=AccountConfigFacts(True, True, True, exists),
        now=-0.5,
    )
    assert plan.localconfig_action == expected
    assert plan.timestamp == -60
    writes = [item for item in plan.writes if item.relative_path == "config/localconfig.vdf"]
    if last_content is None:
        assert not writes
    else:
        expected_content = templates.localconfig if last_content == "template" else last_content
        assert writes[-1].content == expected_content


def test_non_string_restore_is_descriptive_but_cannot_truncate_an_offline_copy(files):
    original = contents(files.workspace.root)
    with pytest.raises(ConfigError, match="mapped_string_required"):
        preview_account_config(
            files.workspace,
            replace(files.settings, no_avast_mode=False, mapped_localconfig=False),
            steam_id=STEAM_ID,
            video_source=files.video,
            machine_source=files.machine,
            now=0,
        )
    assert contents(files.workspace.root) == original


def test_equal_decoded_autoexec_preserves_whole_tree_and_legacy_file(files):
    files.source.write_bytes(b"equal\r\nwith newline\r")
    files.autoexec.write_bytes(b"equal\nwith newline\n")
    original = {path: file_state(path) for path in files.game_cfg.rglob("*") if path.is_file()}
    directory_state = files.game_cfg.stat()
    preview = autoexec_preview(files)
    assert not preview.plan.changed
    result = apply_autoexec(preview)
    assert not result.changed and result.backup_path is None
    assert {path: file_state(path) for path in original} == original
    assert files.game_cfg.stat().st_mtime_ns == directory_state.st_mtime_ns
    assert not list(files.workspace.root.glob(".config-*"))


@pytest.mark.parametrize("changed", ["source", "destination", "legacy", "marker"])
def test_noop_autoexec_revalidates_source_siblings_and_marker(files, changed):
    files.autoexec.write_bytes(files.source.read_bytes())
    preview = autoexec_preview(files)
    assert not preview.plan.changed
    target = {
        "source": files.source,
        "destination": files.autoexec,
        "legacy": files.legacy,
        "marker": files.workspace.root / ".fsm-config-workspace.json",
    }[changed]
    target.write_bytes(target.read_bytes() + b"changed")
    before = contents(files.workspace.root)
    with pytest.raises(ConfigStalePreviewError):
        apply_autoexec(preview)
    assert contents(files.workspace.root) == before


@pytest.mark.parametrize("name", ["../escape", r"..\escape", "CON", "aux.txt", "x:stream", "x."])
def test_autoexec_filename_cannot_expand_its_effect_scope(files, name):
    original = contents(files.workspace.root)
    with pytest.raises(ConfigError, match="invalid_filename"):
        preview_autoexec(files.workspace, source_path=files.source, config_file_name=name)
    assert contents(files.workspace.root) == original


def test_hardlinked_unrelated_account_member_is_rejected_without_mutating_alias(files, tmp_path):
    external = write(tmp_path / "external", b"immutable alias")
    alias = files.cfg / "unrelated-linked-file"
    os.link(external, alias)
    original = file_state(external)
    with pytest.raises(ConfigError, match="hardlink_rejected"):
        account_preview(files)
    assert file_state(external) == original
    assert alias.samefile(external)
    assert not list(files.workspace.root.glob(".config-*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows junction containment")
def test_junction_inserted_after_preview_cannot_redirect_copy_or_cleanup(files, tmp_path):
    preview = account_preview(files)
    external = tmp_path / "external-tree"
    sentinel = write(external / "sentinel", b"outside the owned tree")
    original = file_state(sentinel)
    junction = files.cfg / "new-junction"
    result = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(junction), str(external)],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, "synthetic junction creation failed"
    try:
        assert junction.is_junction()
        with pytest.raises(ConfigStalePreviewError):
            apply_account_config(preview)
        assert file_state(sentinel) == original
        assert not list(files.workspace.root.glob(".config-*"))
    finally:
        junction.rmdir()  # Remove the link itself, never recurse through its target.


@pytest.mark.parametrize("fault", ["io", "stale"])
@pytest.mark.parametrize("rollback_denied", [False, True])
def test_late_directory_failure_rolls_back_or_reports_retained_backup(
    files, monkeypatch, fault, rollback_denied
):
    preview = account_preview(files)
    original = contents(files.account)
    native_replace = configs._replace_bounded

    def fail_install(source, destination, workspace):
        if source.name.startswith(".config-stage-"):
            if fault == "stale":
                raise ConfigStalePreviewError()
            raise OSError("synthetic private install diagnostic")
        if source.name.startswith(".config-backup-") and rollback_denied:
            raise OSError("synthetic private rollback diagnostic")
        return native_replace(source, destination, workspace)

    monkeypatch.setattr(configs, "_replace_bounded", fail_install)
    with pytest.raises(ConfigApplyError) as error:
        apply_account_config(preview)
    assert "synthetic private" not in str(error.value)
    assert error.value.rollback_succeeded is (not rollback_denied)
    if rollback_denied:
        assert not files.account.exists()
        assert error.value.backup_path is not None
        assert contents(error.value.backup_path) == original
    else:
        assert contents(files.account) == original
        assert error.value.backup_path is None
    assert not list(files.workspace.root.glob(".config-stage-*"))


def test_failed_install_never_overwrites_an_independently_created_replacement(files, monkeypatch):
    preview = account_preview(files)
    original = contents(files.account)
    native_replace = configs._replace_bounded

    def replace_with_race(source, destination, workspace):
        if source.name.startswith(".config-stage-"):
            write(destination / "independent", b"preserve the new replacement")
            raise OSError("synthetic destination race")
        return native_replace(source, destination, workspace)

    monkeypatch.setattr(configs, "_replace_bounded", replace_with_race)
    with pytest.raises(ConfigApplyError) as error:
        apply_account_config(preview)
    assert not error.value.rollback_succeeded
    assert contents(files.account) == {"independent": b"preserve the new replacement"}
    assert error.value.backup_path is not None
    assert contents(error.value.backup_path) == original


def test_stage_cleanup_failure_preserves_primary_error_and_recovery_paths(files, monkeypatch):
    preview = account_preview(files)
    original = contents(files.account)
    native_replace, native_remove = configs._replace_bounded, configs._remove_bounded

    def fail_install(source, destination, workspace):
        if source.name.startswith(".config-stage-"):
            raise OSError("synthetic install error")
        return native_replace(source, destination, workspace)

    def fail_stage_remove(path, root):
        if path.name.startswith(".config-stage-"):
            raise OSError("synthetic cleanup error")
        return native_remove(path, root)

    monkeypatch.setattr(configs, "_replace_bounded", fail_install)
    monkeypatch.setattr(configs, "_remove_bounded", fail_stage_remove)
    with pytest.raises(ConfigApplyError) as error:
        apply_account_config(preview)
    assert error.value.rollback_succeeded
    assert contents(files.account) == original
    assert error.value.staging_path is not None and error.value.staging_path.is_dir()
    assert error.value.code == "config_apply_failed"


def test_keyboard_interrupt_restores_original_tree_before_propagating(files, monkeypatch):
    preview = account_preview(files)
    original = contents(files.account)
    native_replace = configs._replace_bounded

    def interrupt_install(source, destination, workspace):
        if source.name.startswith(".config-stage-"):
            raise KeyboardInterrupt
        return native_replace(source, destination, workspace)

    monkeypatch.setattr(configs, "_replace_bounded", interrupt_install)
    with pytest.raises(KeyboardInterrupt):
        apply_account_config(preview)
    assert contents(files.account) == original
    assert not list(files.workspace.root.glob(".config-*"))


def test_failed_stage_reservation_never_deletes_a_preexisting_tree(files, monkeypatch):
    preview = account_preview(files)
    original = contents(files.account)
    stage_id = "a" * 32
    stage = files.workspace.root / (".config-stage-" + stage_id)
    sentinel = write(stage / "preexisting", b"not created by this operation")
    saved = file_state(sentinel)
    monkeypatch.setattr(configs.uuid, "uuid4", lambda: SimpleNamespace(hex=stage_id))
    with pytest.raises(ConfigApplyError):
        apply_account_config(preview)
    assert sentinel.exists(), "cleanup must not delete a directory it did not create"
    assert file_state(sentinel) == saved
    assert contents(files.account) == original


def test_concurrent_autoexec_apply_commits_one_reviewed_tree(files):
    preview = autoexec_preview(files)
    before = contents(files.game_cfg)

    def apply():
        try:
            return apply_autoexec(preview)
        except ConfigStalePreviewError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: apply(), range(2)))
    successful = [result for result in results if result is not None]
    assert len(successful) == 1 and successful[0].changed
    assert not files.legacy.exists()
    assert files.autoexec.read_bytes() == b"synthetic autoexec\r\n"
    assert (files.game_cfg / "nested" / "unrelated.cfg").read_bytes() == b"keep nested"
    backups = list(files.workspace.root.glob(".config-backup-*"))
    assert len(backups) == 1 and contents(backups[0]) == before
