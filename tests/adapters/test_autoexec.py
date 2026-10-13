import os
from dataclasses import replace

import pytest

from fsm_panel.adapters import account_config as config
from fsm_panel.adapters import autoexec


@pytest.fixture
def fixture(tmp_path):
    workspace = config.create_config_workspace(tmp_path / "owned")
    cfg = workspace.game_root / "game/csgo/cfg"
    cfg.mkdir(parents=True)
    source = tmp_path / "source.cfg"
    source.write_bytes(b"new\r\ncontent\n")
    (cfg / "autoexec.cfg").write_bytes(b"old\n")
    (cfg / "legacy.cfg").write_bytes(b"legacy\r\n")
    (cfg / "untouched.cfg").write_bytes(b"keep\r\n")
    return workspace, cfg, source


def preview(fixture, name="legacy.cfg"):
    return autoexec.preview_autoexec(fixture[0], source_path=fixture[2], config_file_name=name)


def test_pure_legacy_mismatch_order_and_failure_groups():
    plan = autoexec.build_autoexec_plan(
        csgo_path=r"C:\fixture",
        config_file_name="chosen.cfg",
        source_content="new",
        existing_content="old",
    )
    assert plan.cfg_path == r"C:\fixture\game\csgo\cfg"
    assert [group.name for group in plan.groups] == [
        "read_source",
        "read_compare_existing",
        "remove_legacy_name",
        "write_autoexec",
    ]
    assert [(op.kind, op.path) for group in plan.groups for op in group.operations] == [
        ("read", r"settings\chosen.cfg"),
        ("read", r"C:\fixture\game\csgo\cfg\autoexec.cfg"),
        ("remove", r"C:\fixture\game\csgo\cfg\autoexec.cfg"),
        ("remove", r"C:\fixture\game\csgo\cfg\chosen.cfg"),
        ("write_and_close", r"C:\fixture\game\csgo\cfg\autoexec.cfg"),
        ("return_True", ""),
    ]
    assert plan.groups[1].catches == "Exception"
    assert "FileNotFoundError" in plan.groups[1].on_error
    assert plan.groups[2].catches == "Exception"
    assert plan.groups[3].catches == "none"
    assert "no_rollback" in plan.groups[3].on_error


def test_equal_plan_returns_before_both_removals():
    plan = autoexec.build_autoexec_plan(
        csgo_path="x",
        config_file_name="legacy.cfg",
        source_content="whole\n",
        existing_content="whole\n",
    )
    assert not plan.changed
    assert all(op.kind != "remove" for group in plan.groups for op in group.operations)
    assert plan.groups[-1].operations[-1].kind == "return_True_before_any_removal"
    assert autoexec.build_autoexec_plan(
        csgo_path="x",
        config_file_name="legacy.cfg",
        source_content="whole\n",
        existing_content="whole",
    ).changed


def test_equality_uses_decoded_newlines_and_preserves_both_files_and_timestamps(fixture):
    workspace, cfg, source = fixture
    (cfg / "autoexec.cfg").write_bytes(b"new\ncontent\r\n")
    before = {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino) for p in cfg.iterdir()
    }
    proposal = preview(fixture)
    assert not proposal.plan.changed
    result = autoexec.apply_autoexec(proposal)
    assert not result.changed and result.backup_path is None
    assert before == {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino) for p in cfg.iterdir()
    }
    assert not list(workspace.root.glob(".config-*"))
    assert source.read_bytes() == b"new\r\ncontent\n"


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("name", ["legacy.cfg", "autoexec.cfg"])
def test_mismatch_or_missing_destination_creates_output_with_backup(fixture, missing, name):
    _, cfg, source = fixture
    if missing:
        (cfg / "autoexec.cfg").unlink()
    before = {p.name: p.read_bytes() for p in cfg.iterdir()}
    result = autoexec.apply_autoexec(preview(fixture, name))
    assert result.changed and result.target_path == cfg / "autoexec.cfg"
    assert (cfg / "autoexec.cfg").read_bytes() == b"new\r\ncontent\r\n"
    assert (cfg / "untouched.cfg").read_bytes() == b"keep\r\n"
    assert (cfg / "legacy.cfg").exists() == (name == "autoexec.cfg")
    assert result.backup_path
    assert before == {p.name: p.read_bytes() for p in result.backup_path.iterdir()}
    assert source.read_bytes() == b"new\r\ncontent\n"


def test_missing_source_does_not_delete_destinations(fixture):
    _, cfg, source = fixture
    source.unlink()
    before = {p.name: p.read_bytes() for p in cfg.iterdir()}
    with pytest.raises(OSError):
        preview(fixture)
    assert before == {p.name: p.read_bytes() for p in cfg.iterdir()}


def test_missing_cfg_directory_is_never_created(tmp_path):
    workspace = config.create_config_workspace(tmp_path / "owned")
    source = tmp_path / "input"
    source.write_text("x")
    with pytest.raises(config.ConfigError, match="config_autoexec_directory_missing"):
        autoexec.preview_autoexec(workspace, source_path=source, config_file_name="chosen.cfg")
    assert not (workspace.game_root / "game").exists()


@pytest.mark.parametrize(
    "name",
    ["../escape", r"..\escape", "C:\\x", "file:stream", "NUL", "bad.", "", ".", "..", "/root"],
)
def test_explicit_filename_cannot_escape(fixture, name):
    with pytest.raises(config.ConfigError, match="config_invalid_filename"):
        preview(fixture, name)


@pytest.mark.parametrize("equal", [False, True])
def test_stale_source_rejected_even_for_noop(fixture, equal):
    _, cfg, source = fixture
    if equal:
        (cfg / "autoexec.cfg").write_bytes(source.read_bytes())
    proposal = preview(fixture)
    source.write_bytes(b"changed")
    with pytest.raises(config.ConfigStalePreviewError):
        autoexec.apply_autoexec(proposal)
    assert (cfg / "legacy.cfg").exists()


def test_unrelated_cfg_change_rejects_stale_tree(fixture):
    proposal = preview(fixture)
    (fixture[1] / "new.cfg").write_bytes(b"new unrelated")
    with pytest.raises(config.ConfigStalePreviewError):
        autoexec.apply_autoexec(proposal)
    assert (fixture[1] / "legacy.cfg").exists()


def test_failed_final_replace_recovers_both_removed_files(fixture, monkeypatch):
    proposal = preview(fixture)
    cfg = fixture[1]
    before = {p.name: p.read_bytes() for p in cfg.iterdir()}
    original = os.replace
    count = 0

    def replacing(source, destination):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("synthetic")
        original(source, destination)

    monkeypatch.setattr(config.os, "replace", replacing)
    with pytest.raises(config.ConfigApplyError) as error:
        autoexec.apply_autoexec(proposal)
    assert error.value.rollback_succeeded
    assert before == {p.name: p.read_bytes() for p in cfg.iterdir()}


def test_plan_source_and_destination_alias_rejected(fixture):
    workspace, cfg, _ = fixture
    with pytest.raises(config.ConfigError, match="config_source_destination_overlap"):
        autoexec.preview_autoexec(
            workspace, source_path=cfg / "autoexec.cfg", config_file_name="legacy.cfg"
        )


def test_forged_removal_is_not_executed(fixture):
    proposal = preview(fixture)
    altered = replace(proposal.transaction, removals=("untouched.cfg",))
    with pytest.raises(config.ConfigStalePreviewError):
        autoexec.apply_autoexec(replace(proposal, transaction=altered))
    assert (fixture[1] / "untouched.cfg").exists()
