import hashlib
import json
import os
import subprocess
from dataclasses import replace
from importlib.resources import files

import pytest

from fsm_panel.adapters import account_config as config
from fsm_panel.adapters.launch_plan import STEAM_ID_BASE


def settings(**changes):
    defaults = config.AccountConfigSettings(
        steam_path=r"C:\synthetic\steam.exe",
        login="fixture-login",
        force=False,
        disable_steam_overlay=False,
        no_avast_mode=True,
        mapped_localconfig=None,
    )
    return replace(defaults, **changes)


def plan(options=None, *, facts=None, now=60.5, **changes):
    return config.build_account_config_plan(
        options or settings(**changes),
        steam_id=STEAM_ID_BASE + 42,
        video="video\n",
        machine="machine\n",
        templates=config.load_config_templates(),
        facts=facts or config.AccountConfigFacts(True, True, True, True),
        now=now,
    )


def write(root, relative, content=b"old"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def tree_bytes(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def fixture(tmp_path):
    workspace = config.create_config_workspace(tmp_path / "owned")
    source = tmp_path / "sources"
    video = write(source, "video.txt", b"video\r\nnext\rfinal\n")
    machine = write(source, "machine.vcfg", b"machine\n")
    target = workspace.steam_root / "userdata/42"
    write(target, "730/remote/delete-me.txt")
    write(target, "730/local/cfg/cs2_user_convars_0_slot0.vcfg")
    write(target, "730/local/cfg/cs2_user_convars_0_slot0.vcfg_lastclouded")
    write(target, "730/local/cfg/keep.vcfg", b"keep-exact\x00")
    write(target, "config/localconfig.vdf", b"existing-local\r\n")
    write(target, "another-app/keep", b"unrelated")
    return workspace, video, machine, target


def preview(fixture, options=None, **changes):
    workspace, video, machine, _ = fixture
    return config.preview_account_config(
        workspace,
        options or settings(**changes),
        steam_id=STEAM_ID_BASE + 42,
        video_source=video,
        machine_source=machine,
        now=61.5,
    )


def test_generic_template_hashes_and_provenance():
    package = files("fsm_panel.config_templates")
    manifest = json.loads(package.joinpath("manifest.json").read_text())
    assert len(manifest) == 5
    for entry in manifest:
        raw = package.joinpath(entry["file"]).read_bytes()
        assert len(raw) == entry["utf8_bytes"]
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
        assert entry["method"]
    templates = config.load_config_templates()
    assert templates.friends_block
    assert templates.remotecache == '"7"\n{\n}\n'
    assert templates.serverbrowser.count("%s") == 1


def test_plan_order_exception_scopes_and_literal_windows_path():
    result = plan()
    assert result.account_path == r"C:\synthetic\userdata\42"
    assert [group.name for group in result.groups] == [
        "preliminary_listdir",
        "preliminary_mkdir",
        "read_sources",
        "remove_remote730",
        "remove_convars",
        "mkdir_remote7",
        "write_app7",
        "prepare_cfg",
        "copy_cfg_finally",
        "localconfig_default",
        "localconfig_restore",
    ]
    by_name = {group.name: group for group in result.groups}
    assert by_name["preliminary_mkdir"].catches == "BaseException"
    assert by_name["read_sources"].catches == "none"
    assert by_name["write_app7"].catches == "BaseException"
    assert by_name["remove_convars"].catches == "Exception"
    assert by_name["remove_convars"].on_error == "skip_remaining_group_continue"
    assert "finally" in by_name["prepare_cfg"].on_error
    assert "pending_prepare_cfg_exception_propagates" in by_name["copy_cfg_finally"].on_error
    assert [operation.path for operation in by_name["read_sources"].operations] == [
        r"settings\cs2_video.txt",
        r"settings\cs2_machine_convars.vcfg",
    ]
    assert plan(settings(steam_path="C:/alternate/steam.exe")).account_path == r"\userdata\42"


def test_preliminary_list_failure_does_not_cancel_writer():
    result = plan(facts=config.AccountConfigFacts(False, False, False, False))
    assert result.log_messages == ("Check your STEAM_PATH!", "[fixture-login] Setting was updated!")
    assert len(result.writes) == 5
    assert "return_from_helper_only" in result.groups[0].on_error


def test_force_changes_only_logs_not_writes_predicates_or_timestamp():
    normal, forced = plan(), plan(force=True)
    assert normal.writes == forced.writes
    assert normal.removals == forced.removals
    assert normal.localconfig_action == forced.localconfig_action
    assert normal.timestamp == forced.timestamp
    assert forced.log_messages == ()
    assert normal.log_messages == (
        "[fixture-login] Already has config folder!",
        "[fixture-login] Settings folder was added!",
        "[fixture-login] Setting was updated!",
    )
    assert plan(
        force=True, facts=config.AccountConfigFacts(False, False, False, False)
    ).log_messages == ("Check your STEAM_PATH!",)


@pytest.mark.parametrize("now,rounded", [(60.5, 0), (61.5, 2), (59.5, 0), (58.5, -2)])
def test_serverbrowser_injected_clock_uses_bankers_round(now, rounded):
    result = plan(now=now)
    assert result.timestamp == rounded
    history = next(w.content for w in result.writes if "serverbrowser" in w.relative_path)
    assert f'"lastupdate"\t\t"{rounded}"' in history


@pytest.mark.parametrize("overlay", [False, True])
@pytest.mark.parametrize("no_avast", [False, True])
@pytest.mark.parametrize("mapped", [None, "", "restored", False])
@pytest.mark.parametrize("existing", [False, True])
def test_exact_localconfig_option_predicates(overlay, no_avast, mapped, existing):
    result = plan(
        disable_steam_overlay=overlay,
        no_avast_mode=no_avast,
        mapped_localconfig=mapped,
        facts=config.AccountConfigFacts(True, True, True, existing),
    )
    selected = [w for w in result.writes if w.relative_path == "config/localconfig.vdf"]
    if not no_avast and mapped is not None:
        assert result.localconfig_action == "restore"
        assert selected[-1].content is mapped
        assert selected[-1].encoding == "utf-8"
    elif overlay or mapped is not None:
        assert result.localconfig_action == ("preserve" if existing else "create")
        assert len(selected) == (0 if existing else 1)
    else:
        assert result.localconfig_action == "untouched"
        assert selected == []
    if existing and (overlay or mapped is not None):
        ops = result.groups[-2].operations
        assert [op.kind for op in ops] == [
            "makedirs_no_exist_ok_ignore_Exception",
            "open_read_close_without_reading",
        ]


def test_plan_repr_hides_inputs():
    result = plan(mapped_localconfig="private-value", no_avast_mode=False)
    assert "private-value" not in repr(result)
    assert "fixture-login" not in repr(result)
    assert "synthetic" not in repr(result)


def test_prepare_preserves_sources_untouched_files_and_existing_localconfig(fixture):
    workspace, video, machine, target = fixture
    original = tree_bytes(target)
    sources = (video.read_bytes(), machine.read_bytes())
    before = preview(fixture, disable_steam_overlay=True)
    assert tree_bytes(target) == original  # Preview writes nothing.
    result = config.apply_account_config(before)
    assert result.changed and result.target_path == target
    assert result.backup_path and tree_bytes(result.backup_path) == original
    assert not (target / "730/remote").exists()
    assert not (target / "730/local/cfg/cs2_user_convars_0_slot0.vcfg").exists()
    assert not (target / "730/local/cfg/cs2_user_convars_0_slot0.vcfg_lastclouded").exists()
    assert (target / "730/local/cfg/keep.vcfg").read_bytes() == b"keep-exact\x00"
    assert (target / "config/localconfig.vdf").read_bytes() == b"existing-local\r\n"
    assert (target / "another-app/keep").read_bytes() == b"unrelated"
    assert (target / "730/local/cfg/cs2_video.txt").read_bytes() == b"video\r\nnext\r\nfinal\r\n"
    assert (target / "730/local/cfg/cs2_machine_convars.vcfg").read_bytes() == b"machine\r\n"
    assert sources == (video.read_bytes(), machine.read_bytes())
    assert config.open_config_workspace(workspace.root) == workspace


def test_new_account_and_default_localconfig(tmp_path):
    workspace = config.create_config_workspace(tmp_path / "copy")
    video = write(tmp_path, "sources/video", b"v")
    machine = write(tmp_path, "sources/machine", b"m")
    proposal = config.preview_account_config(
        workspace,
        settings(disable_steam_overlay=True),
        steam_id=STEAM_ID_BASE - 1,
        video_source=video,
        machine_source=machine,
        now=100,
    )
    result = config.apply_account_config(proposal)
    assert result.backup_path is None
    assert result.target_path.name == "-1"  # No unsigned masking or clamp.
    assert (
        result.target_path / "config/localconfig.vdf"
    ).read_text() == config.load_config_templates().localconfig


def test_empty_mapped_value_is_restored(fixture):
    result = config.apply_account_config(
        preview(fixture, no_avast_mode=False, mapped_localconfig="")
    )
    assert (result.target_path / "config/localconfig.vdf").read_bytes() == b""


def test_mapped_restore_uses_utf8_even_when_source_policy_differs(fixture):
    workspace, video, machine, _ = fixture
    proposal = config.preview_account_config(
        workspace,
        settings(no_avast_mode=False, mapped_localconfig="snowman \u2603\n"),
        steam_id=STEAM_ID_BASE + 42,
        video_source=video,
        machine_source=machine,
        now=90,
        encoding="cp1252",
    )
    result = config.apply_account_config(proposal)
    assert (
        result.target_path / "config/localconfig.vdf"
    ).read_bytes() == "snowman \u2603\r\n".encode()


@pytest.mark.parametrize("mapped", [False, 0, [], {}])
def test_nonstring_mapped_restore_cannot_truncate_original(fixture, mapped):
    before = tree_bytes(fixture[3])
    with pytest.raises(config.ConfigError, match="config_mapped_string_required"):
        preview(fixture, no_avast_mode=False, mapped_localconfig=mapped)
    assert tree_bytes(fixture[3]) == before


@pytest.mark.parametrize("which", ["source", "destination", "unrelated", "marker"])
def test_stale_preview_rejected_without_backup_or_stage(fixture, which):
    workspace, video, _, target = fixture
    proposal = preview(fixture)
    if which == "source":
        video.write_bytes(b"changed")
    elif which == "destination":
        (target / "config/localconfig.vdf").write_bytes(b"changed")
    elif which == "unrelated":
        write(target, "another-app/new", b"changed")
    else:
        (workspace.root / ".fsm-config-workspace.json").write_text("{}")
    changed = tree_bytes(target)
    with pytest.raises(config.ConfigStalePreviewError):
        config.apply_account_config(proposal)
    assert tree_bytes(target) == changed
    assert not list(workspace.root.glob(".config-*"))


def test_forged_preview_writes_are_rejected(fixture):
    proposal = preview(fixture)
    transaction = replace(proposal.transaction, writes=(("../../escape", b"x"),))
    with pytest.raises(config.ConfigStalePreviewError):
        config.apply_account_config(replace(proposal, transaction=transaction))


def test_stale_after_staging_preserves_concurrent_value(fixture, monkeypatch):
    workspace, _, _, target = fixture
    proposal = preview(fixture)
    original_copy = config.shutil.copytree

    def copying(*args, **kwargs):
        result = original_copy(*args, **kwargs)
        (target / "another-app/keep").write_bytes(b"external")
        return result

    monkeypatch.setattr(config.shutil, "copytree", copying)
    with pytest.raises(config.ConfigStalePreviewError):
        config.apply_account_config(proposal)
    assert (target / "another-app/keep").read_bytes() == b"external"
    assert not list(workspace.root.glob(".config-*"))


def test_failed_final_replace_rolls_back_original_tree(fixture, monkeypatch):
    proposal = preview(fixture)
    before = tree_bytes(fixture[3])
    original_replace = os.replace
    calls = 0

    def replacing(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic rename failure")
        original_replace(source, destination)

    monkeypatch.setattr(config.os, "replace", replacing)
    with pytest.raises(config.ConfigApplyError) as error:
        config.apply_account_config(proposal)
    assert calls == 3
    assert error.value.rollback_succeeded
    assert error.value.backup_path is None
    assert tree_bytes(fixture[3]) == before


def test_failed_rollback_retains_recoverable_backup_and_reports_it(fixture, monkeypatch):
    proposal = preview(fixture)
    before = tree_bytes(fixture[3])
    original_replace = os.replace
    calls = 0

    def replacing(source, destination):
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise OSError("synthetic rename failure")
        original_replace(source, destination)

    monkeypatch.setattr(config.os, "replace", replacing)
    with pytest.raises(config.ConfigApplyError) as error:
        config.apply_account_config(proposal)
    assert not error.value.rollback_succeeded
    assert error.value.backup_path is not None
    assert tree_bytes(error.value.backup_path) == before
    assert not fixture[3].exists()


def test_cleanup_failure_never_masks_backup_and_rollback_metadata(fixture, monkeypatch):
    proposal = preview(fixture)
    original_replace = os.replace
    calls = 0

    def replacing(source, destination):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("synthetic")
        original_replace(source, destination)

    original_remove = config._remove_bounded

    def removal(path, root):
        if path.name.startswith(".config-stage-"):
            raise OSError("cleanup")
        return original_remove(path, root)

    monkeypatch.setattr(config.os, "replace", replacing)
    monkeypatch.setattr(config, "_remove_bounded", removal)
    with pytest.raises(config.ConfigApplyError) as error:
        config.apply_account_config(proposal)
    assert error.value.backup_path and error.value.backup_path.exists()
    assert error.value.staging_path and error.value.staging_path.exists()
    assert not error.value.rollback_succeeded


def test_staging_write_failure_leaves_destination_untouched(fixture, monkeypatch):
    proposal = preview(fixture)
    before = tree_bytes(fixture[3])

    def fsync_failure(_):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(config.os, "fsync", fsync_failure)
    with pytest.raises(config.ConfigApplyError) as error:
        config.apply_account_config(proposal)
    assert error.value.rollback_succeeded
    assert error.value.backup_path is None
    assert tree_bytes(fixture[3]) == before


def test_convar_directory_is_not_recursively_removed(fixture):
    convar = fixture[3] / "730/local/cfg/cs2_user_convars_0_slot0.vcfg"
    convar.unlink()
    write(convar, "inside", b"keep")
    proposal = preview(fixture)
    before = tree_bytes(fixture[3])
    with pytest.raises(config.ConfigApplyError):
        config.apply_account_config(proposal)
    assert tree_bytes(fixture[3]) == before


def test_missing_source_does_not_create_account(fixture):
    fixture[1].unlink()
    before = tree_bytes(fixture[3])
    with pytest.raises(OSError):
        preview(fixture)
    assert tree_bytes(fixture[3]) == before


def test_workspace_creation_refuses_existing_original_and_steam_ancestors(tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(config.ConfigError, match="config_new_workspace_required"):
        config.create_config_workspace(existing)
    for name in ("Panel.exe", "steam.exe"):
        root = tmp_path / name.split(".")[0]
        write(root, name)
        with pytest.raises(config.ConfigError):
            config.create_config_workspace(root / "copy")
        assert not (root / "copy").exists()


def test_unmarked_or_tampered_workspace_is_rejected(tmp_path, fixture):
    with pytest.raises(config.ConfigError):
        config.open_config_workspace(tmp_path)
    workspace = fixture[0]
    (workspace.root / ".fsm-config-workspace.json").write_text('{"token": "bad"}')
    with pytest.raises(config.ConfigError):
        config.open_config_workspace(workspace.root)


def test_overlap_and_hardlink_rejected(fixture, tmp_path):
    workspace, video, machine, target = fixture
    inside = write(workspace.root, "source-video", b"inside")
    with pytest.raises(config.ConfigError, match="config_source_destination_overlap"):
        config.preview_account_config(
            workspace,
            settings(),
            steam_id=STEAM_ID_BASE + 42,
            video_source=inside,
            machine_source=machine,
            now=0,
        )
    os.link(video, target / "linked")
    with pytest.raises(config.ConfigError, match="config_hardlink_rejected"):
        preview(fixture)


def test_junction_in_target_tree_is_rejected(fixture, tmp_path):
    target = fixture[3]
    outside = tmp_path / "external"
    outside.mkdir()
    link = target / "junction"
    if os.name == "nt":
        process = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True
        )
        if process.returncode:
            pytest.skip("junction creation unavailable")
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(config.ConfigError, match="config_link_rejected"):
            preview(fixture)
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


def test_bounded_tree_and_source_reads(fixture, monkeypatch):
    monkeypatch.setattr(config, "MAX_FILE_BYTES", 1024)
    fixture[1].write_bytes(b"x" * 1025)
    with pytest.raises(config.ConfigError, match="config_file_too_large"):
        preview(fixture)
    fixture[1].write_bytes(b"v")
    monkeypatch.setattr(config, "MAX_TREE_ENTRIES", 2)
    with pytest.raises(config.ConfigError, match="config_tree_too_large"):
        preview(fixture)
