import hashlib
import json
import os
import re
import socket
from dataclasses import replace
from importlib.resources import files
from pathlib import Path

import pytest

from fsm_panel.adapters import account_config as config
from fsm_panel.adapters import autoexec
from fsm_panel.adapters import gsi_config as gsi

# Synthetic public data only. Never copy the shipped authentication literal.
TOKEN = "Synthetic_GSI_token_0123456789-abcDEF"
NEXT_TOKEN = "Another_synthetic_token_9876543210-X"
FILENAME = "gamestate_integration_3.cfg"


@pytest.fixture
def fixture(tmp_path):
    workspace = config.create_config_workspace(tmp_path / "owned")
    cfg = workspace.game_root / "game/csgo/cfg"
    cfg.mkdir(parents=True)
    source = tmp_path / "fresh-token.txt"
    source.write_bytes(TOKEN.encode("ascii"))
    (cfg / FILENAME).write_bytes(b"old indexed config\r\n")
    (cfg / "gamestate_integration_1.cfg").write_bytes(b"preserve other index\n")
    (cfg / "autoexec.cfg").write_bytes(b"preserve autoexec\r\n")
    (cfg / "nested").mkdir()
    (cfg / "nested/other.cfg").write_bytes(b"nested bytes\x00")
    return workspace, cfg, source


def preview(fixture, **overrides):
    values = {
        "running_on_port": 12345,
        "gsi_file_index": 3,
        "token_source": fixture[2],
    }
    values.update(overrides)
    return gsi.preview_gsi_config(fixture[0], **values)


def tree_bytes(path):
    return {
        item.relative_to(path).as_posix(): item.read_bytes()
        for item in path.rglob("*")
        if item.is_file()
    }


def test_packaged_template_identity_and_exact_non_auth_fields():
    package = files("fsm_panel.gsi_templates")
    raw = package.joinpath("gsi.sanitized.template").read_bytes()
    manifest = json.loads(package.joinpath("manifest.json").read_text("utf-8"))
    assert len(raw) == manifest["utf8_bytes"] == 1268
    assert hashlib.sha256(raw).hexdigest() == manifest["sha256"] == gsi.GSI_TEMPLATE_SHA256
    assert manifest["source_sha256"] == (
        "3731a48590e3169071b81aab9338a36b38a788133adfd43fada99ad3add1f308"
    )
    template = gsi.load_gsi_template()
    assert template.encode("utf-8") == raw
    assert template.startswith('"Observer Players"\n')
    assert not template.endswith("\n")
    pairs = re.findall(r'"([^"]+)"\s+"([^"]+)"', template)
    assert pairs == [
        ("uri", "http://127.0.0.1:PORT_PLACEHOLDER"),
        ("timeout", "1.0"),
        ("buffer", "0"),
        ("throttle", "0"),
        ("heartbeat", "20.0"),
        ("token", "TOKEN_PLACEHOLDER"),
        ("precision_time", "3"),
        ("precision_position", "3"),
        ("precision_vector", "3"),
        ("provider", "1"),
        ("map", "1"),
        ("round", "1"),
        ("player_id", "1"),
        ("player_state", "1"),
        ("allplayers_id", "1"),
        ("allplayers_state", "1"),
        ("allgrenades", "1"),
        ("allplayers_match_stats", "1"),
        ("allplayers_position", "1"),
        ("allplayers_weapons", "1"),
        ("bomb", "1"),
        ("map_round_wins", "1"),
        ("phase_countdowns", "1"),
    ]


def test_corrupt_or_missing_template_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(gsi, "files", lambda package: tmp_path)
    with pytest.raises(config.ConfigError, match="gsi_template_unavailable"):
        gsi.load_gsi_template()
    (tmp_path / "gsi.sanitized.template").write_bytes(b"x" * 1268)
    with pytest.raises(config.ConfigError, match="gsi_template_hash_mismatch"):
        gsi.load_gsi_template()


def test_legacy_plan_is_inert_preserves_replacement_and_exception_scopes(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("A pure legacy plan attempted filesystem or network IO")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    plan = gsi.build_legacy_gsi_plan(
        csgo_path=r"C:\logical\game",
        running_on_port="no validation",
        gsi_file_index="old-index",
        template="PORT_PLACEHOLDER TOKEN_PLACEHOLDER PORT_PLACEHOLDER",
    )
    target = r"C:\logical\game\game\csgo\cfg\gamestate_integration_old-index.cfg"
    assert plan.target_path == target
    assert plan.content == "no validation TOKEN_PLACEHOLDER no validation"
    assert plan.always_rewrite
    assert not plan.creates_directory
    assert not plan.deletes_old_indices
    assert [group.name for group in plan.groups] == [
        "render_before_local_handler",
        "format_join_open_write",
    ]
    assert plan.groups[0].catches == "none"
    assert plan.groups[0].on_error == "outer_launch_error_path"
    assert plan.groups[1].catches == "Exception"
    assert plan.groups[1].on_error == (
        "log_Wrong_CS2_path_then_continue; log_failure_can_propagate"
    )
    assert [(op.kind, op.path) for group in plan.groups for op in group.operations] == [
        ("str_running_on_port", ""),
        ("replace_all_PORT_PLACEHOLDER", ""),
        ("format_index_empty_spec", ""),
        ("join_filename", target),
        ("open_context_mode_w", target),
        ("write", target),
    ]
    assert plan.groups[1].operations[-1].content == plan.content
    assert all(op.encoding == "legacy-default" for group in plan.groups for op in group.operations)


@pytest.mark.parametrize("value", [None, True, False, -2, 0, 65536, 1.5, "a/b"])
def test_legacy_scalar_stringification_and_unvalidated_path_are_descriptive(value):
    assert gsi.render_legacy_gsi_content("PORT_PLACEHOLDER", value) == str(value)
    assert gsi.legacy_gsi_config_path(r"C:\x\..", value) == (
        "C:\\x\\..\\game\\csgo\\cfg\\gamestate_integration_" + format(value, "") + ".cfg"
    )


def test_legacy_custom_object_methods_are_never_executed():
    class Trap:
        def __str__(self):
            pytest.fail("custom __str__ executed")

        def __format__(self, spec):
            pytest.fail("custom __format__ executed")

    with pytest.raises(config.ConfigError, match="gsi_legacy_scalar_required"):
        gsi.render_legacy_gsi_content("PORT_PLACEHOLDER", Trap())
    with pytest.raises(config.ConfigError, match="gsi_legacy_scalar_required"):
        gsi.legacy_gsi_config_path("x", Trap())
    with pytest.raises(config.ConfigError, match="gsi_template_string_required"):
        gsi.render_legacy_gsi_content(Trap(), 1234)
    with pytest.raises(config.ConfigError, match="gsi_path_string_required"):
        gsi.legacy_gsi_config_path(Trap(), 1)


@pytest.mark.parametrize("offset", range(10))
def test_selection_stops_at_first_supplied_exception_outcome(offset, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Port selection tried a network operation")

    monkeypatch.setattr(socket, "socket", forbidden)
    result = gsi.select_legacy_gsi_port([True] * offset + [False, object()])
    assert result.running_on_port == 6969 + offset
    assert result.gsi_file_index == offset + 1
    assert result.probes_consumed == offset + 1
    assert not result.all_probes_completed


def test_all_completed_probe_expressions_keep_original_default():
    result = gsi.select_legacy_gsi_port((True,) * 10)
    assert result == gsi.LegacyGsiSelection(6969, 1, 10, True)


@pytest.mark.parametrize(
    ("outcomes", "error"),
    [
        (iter([False]), "gsi_probe_results_required"),
        ([], "gsi_probe_results_incomplete"),
        ([True] * 9, "gsi_probe_results_incomplete"),
        ([200], "gsi_probe_boolean_required"),
        ([True, None], "gsi_probe_boolean_required"),
    ],
)
def test_selection_rejects_incomplete_or_non_boolean_consumed_outcomes(outcomes, error):
    with pytest.raises(config.ConfigError, match=error):
        gsi.select_legacy_gsi_port(outcomes)


@pytest.mark.parametrize("port", [1, 6969, 6978, 65535])
@pytest.mark.parametrize("token", [TOKEN, "a" * 32, "Z" * 256])
def test_safe_render_uses_explicit_loopback_port_and_caller_token(port, token):
    content = gsi.render_gsi_config(port, token)
    assert f'"uri" "http://127.0.0.1:{port}"' in content
    assert f'"token" "{token}"' in content
    assert "PORT_PLACEHOLDER" not in content
    assert "TOKEN_PLACEHOLDER" not in content
    assert (
        content.replace(f"http://127.0.0.1:{port}", "http://127.0.0.1:PORT_PLACEHOLDER", 1).replace(
            token, "TOKEN_PLACEHOLDER", 1
        )
        == gsi.load_gsi_template()
    )


@pytest.mark.parametrize("port", [True, False, 0, -1, 65536, 6969.0, "6969", None])
def test_safe_renderer_rejects_invalid_port(port):
    with pytest.raises(config.ConfigError, match="gsi_port_invalid"):
        gsi.render_gsi_config(port, TOKEN)


@pytest.mark.parametrize(
    "token",
    [
        None,
        b"x" * 32,
        "",
        "a" * 31,
        "a" * 257,
        TOKEN + '"',
        TOKEN + "\\",
        TOKEN + "/",
        TOKEN + "\0",
        TOKEN + "\t",
        TOKEN + "\r",
        TOKEN + "\n",
        TOKEN + "\r\n",
        TOKEN + " ",
        TOKEN + "é",
        TOKEN + "=",
        TOKEN + "+",
        TOKEN + ".",
        TOKEN + "~",
    ],
)
def test_safe_renderer_rejects_non_urlsafe_token_without_echoing_it(token):
    with pytest.raises(config.ConfigError) as error:
        gsi.render_gsi_config(6969, token)
    assert str(error.value) == "gsi_token_invalid"
    assert repr(token) not in str(error.value)


@pytest.mark.parametrize("suffix", [b"", b"\n", b"\r\n"])
def test_preview_is_read_only_and_accepts_one_final_token_line_ending(fixture, suffix):
    workspace, cfg, source = fixture
    source.write_bytes(TOKEN.encode("ascii") + suffix)
    before = tree_bytes(workspace.root)
    proposal = preview(fixture)
    assert tree_bytes(workspace.root) == before
    assert source.read_bytes() == TOKEN.encode("ascii") + suffix
    assert proposal.target_path == cfg / FILENAME
    assert proposal.running_on_port == 12345
    assert proposal.gsi_file_index == 3
    assert proposal.plan.content == gsi.render_gsi_config(12345, TOKEN)
    assert TOKEN not in repr(proposal)
    assert str(source) not in repr(proposal)


@pytest.mark.parametrize("suffix", [b"\r", b"\n\n", b"\r\n\n", b"\r\n\r\n", b" \n"])
def test_token_file_does_not_trim_extra_whitespace(fixture, suffix):
    fixture[2].write_bytes(TOKEN.encode("ascii") + suffix)
    with pytest.raises(config.ConfigError, match="gsi_token_invalid"):
        preview(fixture)


@pytest.mark.parametrize("index", [True, False, 0, -1, 11, 1.0, "3", "../x", None])
def test_safe_index_is_bounded_independently_of_port(fixture, index):
    with pytest.raises(config.ConfigError, match="gsi_index_invalid"):
        preview(fixture, gsi_file_index=index)


@pytest.mark.parametrize("missing", [False, True])
def test_apply_always_rewrites_and_retains_other_indices_and_full_backup(fixture, missing):
    workspace, cfg, source = fixture
    if missing:
        (cfg / FILENAME).unlink()
    before = tree_bytes(cfg)
    result = gsi.apply_gsi_config(preview(fixture))
    expected = gsi.render_gsi_config(12345, TOKEN).replace("\n", "\r\n").encode("utf-8")
    assert result.changed and result.target_path == cfg / FILENAME
    assert result.backup_path and tree_bytes(result.backup_path) == before
    assert tree_bytes(cfg) == {**before, FILENAME: expected}
    assert source.read_bytes() == TOKEN.encode("ascii")
    assert not list(workspace.root.glob(".config-stage-*"))
    # Identical content remains a transaction and produces another backup.
    original_identity = (cfg / FILENAME).stat().st_ino
    again = gsi.apply_gsi_config(preview(fixture))
    assert again.changed and again.backup_path != result.backup_path
    assert again.backup_path and tree_bytes(again.backup_path) == tree_bytes(cfg)
    assert (cfg / FILENAME).stat().st_ino != original_identity


def test_explicit_encoding_is_used_for_input_and_deterministic_crlf_output(fixture):
    fixture[2].write_bytes((TOKEN + "\r\n").encode("utf-16"))
    result = gsi.apply_gsi_config(preview(fixture, encoding="utf-16"))
    assert result.target_path.read_bytes() == (
        gsi.render_gsi_config(12345, TOKEN).replace("\n", "\r\n").encode("utf-16")
    )


@pytest.mark.parametrize("raw", [b"\xff", b"\xef\xbb\xbf" + TOKEN.encode("ascii")])
def test_invalid_encoding_or_unrequested_bom_does_not_write(fixture, raw):
    before = tree_bytes(fixture[1])
    fixture[2].write_bytes(raw)
    with pytest.raises(config.ConfigError):
        preview(fixture)
    assert tree_bytes(fixture[1]) == before


def test_missing_source_or_unknown_encoding_does_not_mutate_destination(fixture):
    before = tree_bytes(fixture[1])
    with pytest.raises(config.ConfigError, match="config_decode_failed"):
        preview(fixture, encoding="not-a-codec")
    fixture[2].unlink()
    with pytest.raises(OSError):
        preview(fixture)
    assert tree_bytes(fixture[1]) == before


def test_missing_cfg_directory_is_not_created(tmp_path):
    workspace = config.create_config_workspace(tmp_path / "owned")
    source = tmp_path / "token"
    source.write_bytes(TOKEN.encode("ascii"))
    with pytest.raises(config.ConfigError, match="gsi_config_directory_missing"):
        gsi.preview_gsi_config(
            workspace,
            running_on_port=6969,
            gsi_file_index=1,
            token_source=source,
        )
    assert not (workspace.game_root / "game").exists()


def test_source_within_workspace_is_rejected(fixture):
    source = fixture[0].root / "inside-token"
    source.write_bytes(TOKEN.encode("ascii"))
    with pytest.raises(config.ConfigError, match="config_source_destination_overlap"):
        preview(fixture, token_source=source)


@pytest.mark.parametrize("location", ["source", "destination", "unrelated"])
def test_hardlinks_are_rejected_before_any_write(fixture, tmp_path, location):
    _, cfg, source = fixture
    target = {"source": source, "destination": cfg / FILENAME, "unrelated": cfg / "autoexec.cfg"}
    os.link(target[location], tmp_path / "outside-alias")
    before = tree_bytes(cfg)
    with pytest.raises(config.ConfigError, match="config_hardlink_rejected"):
        preview(fixture)
    assert tree_bytes(cfg) == before


@pytest.mark.parametrize("location", ["source", "destination"])
def test_symbolic_links_are_rejected_before_any_write(fixture, tmp_path, location):
    _, cfg, source = fixture
    link = tmp_path / "source-alias" if location == "source" else cfg / "alias.cfg"
    try:
        link.symlink_to(source)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("Creating symbolic links requires Windows Developer Mode or privilege")
        raise
    before = tree_bytes(cfg)
    with pytest.raises(config.ConfigError, match="config_link_rejected"):
        preview(fixture, token_source=link if location == "source" else source)
    assert tree_bytes(cfg) == before


@pytest.mark.parametrize(
    "change", ["token", "newline", "identity", "deleted", "destination", "other"]
)
def test_stale_source_and_full_tree_changes_reject_apply(fixture, change):
    workspace, cfg, source = fixture
    proposal = preview(fixture)
    if change == "token":
        source.write_bytes(NEXT_TOKEN.encode("ascii"))
    elif change == "newline":
        source.write_bytes(TOKEN.encode("ascii") + b"\n")
    elif change == "identity":
        old = source.with_name("held-original")
        source.rename(old)
        source.write_bytes(old.read_bytes())
        os.utime(source, ns=(old.stat().st_atime_ns, old.stat().st_mtime_ns))
    elif change == "deleted":
        source.unlink()
    elif change == "destination":
        (cfg / FILENAME).write_bytes(b"independent edit")
    else:
        (cfg / "nested/other.cfg").write_bytes(b"independent edit")
    before = tree_bytes(cfg)
    with pytest.raises(config.ConfigStalePreviewError):
        gsi.apply_gsi_config(proposal)
    assert tree_bytes(cfg) == before
    assert not list(workspace.root.glob(".config-*"))


def test_source_change_during_preview_collection_is_detected(fixture, monkeypatch):
    original = gsi._snapshot_tree
    calls = 0

    def snapshot(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            fixture[2].write_bytes(NEXT_TOKEN.encode("ascii"))
        return original(path)

    monkeypatch.setattr(gsi, "_snapshot_tree", snapshot)
    with pytest.raises(config.ConfigStalePreviewError):
        preview(fixture)


def test_final_verification_rejects_token_change_after_staging(fixture, monkeypatch):
    workspace, cfg, source = fixture
    proposal = preview(fixture)
    before = tree_bytes(cfg)
    original = config.shutil.copytree

    def copying(*args, **kwargs):
        result = original(*args, **kwargs)
        source.write_bytes(NEXT_TOKEN.encode("ascii"))
        return result

    monkeypatch.setattr(config.shutil, "copytree", copying)
    with pytest.raises(config.ConfigStalePreviewError):
        gsi.apply_gsi_config(proposal)
    assert tree_bytes(cfg) == before
    assert not list(workspace.root.glob(".config-*"))


def test_autoexec_transaction_invalidates_gsi_preview(fixture, tmp_path):
    proposal = preview(fixture)
    other_source = tmp_path / "autoexec-input"
    other_source.write_bytes(b"updated by another adapter\n")
    autoexec.apply_autoexec(
        autoexec.preview_autoexec(
            fixture[0],
            source_path=other_source,
            config_file_name="legacy.cfg",
        )
    )
    before = tree_bytes(fixture[1])
    with pytest.raises(config.ConfigStalePreviewError):
        gsi.apply_gsi_config(proposal)
    assert tree_bytes(fixture[1]) == before


def test_forged_write_or_removal_cannot_bypass_preview_verification(fixture):
    proposal = preview(fixture)
    before = tree_bytes(fixture[1])
    for altered in (
        replace(proposal.transaction, removals=("autoexec.cfg",)),
        replace(proposal.transaction, writes=(("../escape.cfg", b"wrong"),)),
        replace(proposal.transaction, target_relative="steam/userdata"),
    ):
        with pytest.raises(config.ConfigStalePreviewError):
            gsi.apply_gsi_config(replace(proposal, transaction=altered))
    assert tree_bytes(fixture[1]) == before


@pytest.mark.parametrize("failure_at", [1, 2])
def test_failed_replace_restores_original_tree(fixture, monkeypatch, failure_at):
    workspace, cfg, _ = fixture
    proposal = preview(fixture)
    before = tree_bytes(cfg)
    original = os.replace
    calls = 0

    def replacing(source, destination):
        nonlocal calls
        calls += 1
        if calls == failure_at:
            raise OSError("synthetic failure")
        original(source, destination)

    monkeypatch.setattr(config.os, "replace", replacing)
    with pytest.raises(config.ConfigApplyError) as error:
        gsi.apply_gsi_config(proposal)
    assert error.value.rollback_succeeded
    assert str(error.value) == "config_apply_failed"
    assert tree_bytes(cfg) == before
    assert not list(workspace.root.glob(".config-*"))


def test_rollback_failure_retains_recoverable_original_backup(fixture, monkeypatch):
    workspace, cfg, _ = fixture
    proposal = preview(fixture)
    before = tree_bytes(cfg)
    original = os.replace
    calls = 0

    def replacing(source, destination):
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise OSError("synthetic failure")
        original(source, destination)

    monkeypatch.setattr(config.os, "replace", replacing)
    with pytest.raises(config.ConfigApplyError) as error:
        gsi.apply_gsi_config(proposal)
    assert not error.value.rollback_succeeded
    assert str(error.value) == "config_rollback_failed"
    assert error.value.backup_path and tree_bytes(error.value.backup_path) == before
    assert not cfg.exists()
    assert not list(workspace.root.glob(".config-stage-*"))


def test_shared_config_lock_covers_validation_and_commit(fixture, monkeypatch):
    original = gsi._verify_gsi
    calls = 0

    def verify(proposal):
        nonlocal calls
        calls += 1
        assert gsi._LOCK is config._LOCK
        assert config._LOCK._is_owned()
        original(proposal)

    monkeypatch.setattr(gsi, "_verify_gsi", verify)
    gsi.apply_gsi_config(preview(fixture))
    assert calls == 2
