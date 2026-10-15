"""Independent GSI static-contract and isolated transaction boundary checks."""

import concurrent.futures
import hashlib
import itertools
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from fsm_panel import config_cli
from fsm_panel.adapters import account_config as configs
from fsm_panel.adapters import autoexec
from fsm_panel.adapters import gsi_config as gsi

TOKEN = "QA_PRIVATE_GSI_TOKEN_0123456789-abcdefgh"
NEXT_TOKEN = "QA_SECOND_GSI_TOKEN_9876543210-abcdefgh"
PRIVATE_ERROR = "QA_PRIVATE_GSI_ERROR_SENTINEL"
FILENAME = "gamestate_integration_8.cfg"


def tree(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.fixture
def inputs(tmp_path):
    workspace = configs.create_config_workspace(tmp_path / "owned")
    cfg = workspace.game_root / "game/csgo/cfg"
    cfg.mkdir(parents=True)
    (cfg / FILENAME).write_bytes(b"old selected file\r\n")
    (cfg / "gamestate_integration_1.cfg").write_bytes(b"earlier index and token\n")
    (cfg / "autoexec.cfg").write_bytes(b"old autoexec\r\n")
    (cfg / "legacy.cfg").write_bytes(b"preserve until autoexec apply\n")
    (cfg / "nested").mkdir()
    (cfg / "nested/unrelated").write_bytes(b"preserve unrelated data\x00")
    token = tmp_path / "private-token-source"
    token.write_bytes(TOKEN.encode("ascii"))
    other_source = tmp_path / "autoexec-source"
    other_source.write_bytes(b"new synthetic autoexec\n")
    return SimpleNamespace(
        workspace=workspace,
        root=workspace.root,
        cfg=cfg,
        token=token,
        other_source=other_source,
    )


def preview(inputs, **updates):
    options = {"running_on_port": 65535, "gsi_file_index": 8, "token_source": inputs.token}
    options.update(updates)
    return gsi.preview_gsi_config(inputs.workspace, **options)


def test_exhaustive_ten_probe_outcomes_stop_on_exception_and_never_allocate_a_port(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("pure GSI selector attempted host IO")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    for outcomes in itertools.product((False, True), repeat=10):
        completed = all(outcomes)
        offset = 0 if completed else outcomes.index(False)
        result = gsi.select_legacy_gsi_port([*outcomes, object()])
        assert result.running_on_port == 6969 + offset
        assert result.gsi_file_index == 1 + offset
        assert result.probes_consumed == (10 if completed else 1 + offset)
        assert result.all_probes_completed is completed
    # An eleventh failing expression is outside the recovered range.
    result = gsi.select_legacy_gsi_port([True] * 10 + [False])
    assert (result.running_on_port, result.gsi_file_index, result.probes_consumed) == (6969, 1, 10)


@pytest.mark.parametrize("status", [200, 404, 500])
def test_http_status_codes_cannot_be_mistaken_for_probe_expression_outcomes(status):
    with pytest.raises(configs.ConfigError) as caught:
        gsi.select_legacy_gsi_port([True] * 9 + [status])
    assert str(caught.value) == "gsi_probe_boolean_required"
    # Once an earlier expression raised, subsequent status values are irrelevant.
    result = gsi.select_legacy_gsi_port([False, status])
    assert result.running_on_port == 6969 and result.probes_consumed == 1


def test_literal_render_and_filename_use_independent_values_without_normalization(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("pure GSI writer plan attempted host IO")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    result = gsi.build_legacy_gsi_plan(
        csgo_path=r"Z:\synthetic\..\game",
        running_on_port="PORT_PLACEHOLDER-suffix",
        gsi_file_index=-0.0,
        template="PORT_PLACEHOLDER\nTOKEN_PLACEHOLDER\nPORT_PLACEHOLDER",
    )
    assert result.content == "PORT_PLACEHOLDER-suffix\nTOKEN_PLACEHOLDER\nPORT_PLACEHOLDER-suffix"
    assert result.target_path == (
        r"Z:\synthetic\..\game\game\csgo\cfg\gamestate_integration_-0.0.cfg"
    )
    assert result.groups[0].catches == "none"
    assert result.groups[1].catches == "Exception"
    assert "log_failure_can_propagate" in result.groups[1].on_error
    assert result.always_rewrite and not result.creates_directory and not result.deletes_old_indices


def test_sanitized_resource_matches_independently_recorded_export_hash():
    # Hash and size are from the sanitized native export, not adapter constants.
    raw = gsi.load_gsi_template().encode("utf-8")
    assert len(raw) == 1268
    assert hashlib.sha256(raw).hexdigest() == (
        "6b02e3f2bdc3afef44437795cb36ea5c1096e1ec980b91f3d55ab2e40f84ed8f"
    )
    assert raw.count(b"TOKEN_PLACEHOLDER") == 1
    assert raw.count(b"PORT_PLACEHOLDER") == 1
    assert TOKEN.encode() not in raw


@pytest.mark.parametrize(
    "suffix",
    ['"\n}\n"uri" "https://synthetic.invalid/"', '\\"}\n{', '\r\n"data" {', "\x00hidden"],
)
def test_structural_token_injection_never_reaches_output_or_diagnostic(inputs, suffix):
    attempted = TOKEN + suffix
    inputs.token.write_text(attempted, encoding="utf-8")
    before = tree(inputs.root)
    with pytest.raises(configs.ConfigError) as caught:
        preview(inputs)
    assert str(caught.value) == "gsi_token_invalid"
    assert TOKEN not in repr(caught.value)
    assert tree(inputs.root) == before


def test_placeholder_text_inside_a_safe_caller_token_is_not_recursively_substituted(inputs):
    marker_token = "TOKEN_PLACEHOLDER_PORT_PLACEHOLDER_0123456789"
    inputs.token.write_text(marker_token, encoding="ascii")
    proposal = preview(inputs)
    assert f'"token" "{marker_token}"' in proposal.plan.content
    assert '"uri" "http://127.0.0.1:65535"' in proposal.plan.content
    result = gsi.apply_gsi_config(proposal)
    assert result.target_path.name == FILENAME
    assert marker_token in result.target_path.read_text()
    assert marker_token not in repr(proposal)


def test_successive_indices_preserve_old_tokens_and_exact_previous_trees(inputs):
    initial = tree(inputs.cfg)
    source_before = inputs.token.read_bytes()
    first = gsi.apply_gsi_config(preview(inputs, running_on_port=6969, gsi_file_index=10))
    assert first.target_path.name == "gamestate_integration_10.cfg"
    assert tree(first.backup_path) == initial
    first_tree = tree(inputs.cfg)
    inputs.token.write_bytes(NEXT_TOKEN.encode())
    second = gsi.apply_gsi_config(preview(inputs, running_on_port=6978, gsi_file_index=1))
    assert second.target_path.name == "gamestate_integration_1.cfg"
    assert second.backup_path != first.backup_path
    assert tree(second.backup_path) == first_tree
    assert inputs.token.read_bytes() == NEXT_TOKEN.encode()
    assert source_before == TOKEN.encode()
    assert TOKEN in first.target_path.read_text()
    assert NEXT_TOKEN in second.target_path.read_text()
    assert (inputs.cfg / FILENAME).read_bytes() == initial[FILENAME]
    assert (inputs.cfg / "nested/unrelated").read_bytes() == initial["nested/unrelated"]
    assert TOKEN not in repr(first) and NEXT_TOKEN not in repr(second)


@pytest.mark.parametrize("member", ["token", "selected", "unrelated"])
def test_same_size_same_timestamp_edit_still_invalidates_the_hash_snapshot(inputs, member):
    proposal = preview(inputs)
    selected = {
        "token": inputs.token,
        "selected": inputs.cfg / FILENAME,
        "unrelated": inputs.cfg / "nested/unrelated",
    }[member]
    state = selected.stat()
    old = selected.read_bytes()
    selected.write_bytes(old[::-1])
    os.utime(selected, ns=(state.st_atime_ns, state.st_mtime_ns))
    assert selected.stat().st_ino == state.st_ino
    before = tree(inputs.cfg)
    with pytest.raises(configs.ConfigStalePreviewError):
        gsi.apply_gsi_config(proposal)
    assert tree(inputs.cfg) == before
    assert not list(inputs.root.glob(".config-*"))


@pytest.mark.parametrize("member", ["selected", "unrelated", "directory"])
def test_unchanged_target_bytes_with_new_identity_invalidate_preview(inputs, member, tmp_path):
    proposal = preview(inputs)
    selected = {
        "selected": inputs.cfg / FILENAME,
        "unrelated": inputs.cfg / "nested/unrelated",
        "directory": inputs.cfg,
    }[member]
    state = selected.stat()
    replacement = tmp_path / "replacement"
    if selected.is_dir():
        configs.shutil.copytree(selected, replacement, copy_function=configs.shutil.copy2)
        selected.rename(tmp_path / "retained-original-directory")
    else:
        replacement.write_bytes(selected.read_bytes())
    os.utime(replacement, ns=(state.st_atime_ns, state.st_mtime_ns))
    os.replace(replacement, selected)
    assert selected.stat().st_ino != state.st_ino
    before = tree(inputs.cfg)
    with pytest.raises(configs.ConfigStalePreviewError):
        gsi.apply_gsi_config(proposal)
    assert tree(inputs.cfg) == before
    assert not list(inputs.root.glob(".config-*"))


def test_token_identity_changed_after_real_staging_is_revalidated(inputs, monkeypatch):
    proposal = preview(inputs)
    before = tree(inputs.cfg)
    original = configs.shutil.copytree
    changed = []

    def copying(source, destination, *args, **kwargs):
        result = original(source, destination, *args, **kwargs)
        if Path(destination).parent == inputs.root:
            state = inputs.token.stat()
            replacement = inputs.token.with_suffix(".replacement")
            replacement.write_bytes(inputs.token.read_bytes())
            os.utime(replacement, ns=(state.st_atime_ns, state.st_mtime_ns))
            os.replace(replacement, inputs.token)
            changed.append(True)
        return result

    monkeypatch.setattr(configs.shutil, "copytree", copying)
    with pytest.raises(configs.ConfigStalePreviewError):
        gsi.apply_gsi_config(proposal)
    assert changed == [True]
    assert tree(inputs.cfg) == before
    assert not list(inputs.root.glob(".config-*"))


def test_hardlink_added_to_token_after_preview_cannot_change_either_alias(inputs, tmp_path):
    proposal = preview(inputs)
    alias = tmp_path / "external-token-alias"
    os.link(inputs.token, alias)
    before = tree(inputs.cfg)
    with pytest.raises(configs.ConfigStalePreviewError):
        gsi.apply_gsi_config(proposal)
    assert inputs.token.read_bytes() == alias.read_bytes() == TOKEN.encode()
    assert tree(inputs.cfg) == before
    assert not list(inputs.root.glob(".config-*"))


@pytest.mark.parametrize("rollback_denied", [False, True])
def test_stale_error_after_original_move_preserves_recovery_metadata(
    inputs, monkeypatch, rollback_denied
):
    proposal = preview(inputs)
    before = tree(inputs.cfg)
    original = configs._replace_bounded

    def fail_late(source, destination, workspace):
        if source.name.startswith(".config-stage-"):
            assert not inputs.cfg.exists()
            raise configs.ConfigStalePreviewError()
        if rollback_denied and source.name.startswith(".config-backup-"):
            raise PermissionError(PRIVATE_ERROR)
        return original(source, destination, workspace)

    monkeypatch.setattr(configs, "_replace_bounded", fail_late)
    with pytest.raises(configs.ConfigApplyError) as caught:
        gsi.apply_gsi_config(proposal)
    error = caught.value
    assert error.rollback_succeeded is (not rollback_denied)
    assert error.staging_path is None
    assert PRIVATE_ERROR not in str(error)
    if rollback_denied:
        assert error.code == "config_rollback_failed"
        assert not inputs.cfg.exists()
        assert tree(error.backup_path) == before
    else:
        assert error.code == "config_apply_failed"
        assert error.backup_path is None
        assert tree(inputs.cfg) == before


def test_concurrent_gsi_and_autoexec_applies_commit_only_one_reviewed_cfg_tree(inputs, monkeypatch):
    proposed_gsi = preview(inputs)
    proposed_autoexec = autoexec.preview_autoexec(
        inputs.workspace, source_path=inputs.other_source, config_file_name="legacy.cfg"
    )
    before = tree(inputs.cfg)
    entered, release, competitor_started = (threading.Event() for _ in range(3))
    original = configs.shutil.copytree

    def delayed_copy(source, destination, *args, **kwargs):
        result = original(source, destination, *args, **kwargs)
        if Path(destination).parent == inputs.root:
            entered.set()
            assert release.wait(5)
        return result

    def other_apply():
        competitor_started.set()
        return autoexec.apply_autoexec(proposed_autoexec)

    monkeypatch.setattr(configs.shutil, "copytree", delayed_copy)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        winner = executor.submit(gsi.apply_gsi_config, proposed_gsi)
        assert entered.wait(5)
        competitor = executor.submit(other_apply)
        try:
            assert competitor_started.wait(5)
            assert not competitor.done()
            assert tree(inputs.cfg) == before
        finally:
            release.set()
        result = winner.result(timeout=5)
        with pytest.raises(configs.ConfigStalePreviewError):
            competitor.result(timeout=5)
    assert result.changed and tree(result.backup_path) == before
    after = tree(inputs.cfg)
    assert after.pop(FILENAME) != before.pop(FILENAME)
    assert after == before
    assert len(list(inputs.root.glob(".config-backup-*"))) == 1
    assert not list(inputs.root.glob(".config-stage-*"))


def command(inputs):
    return [
        "gsi",
        "--workspace",
        str(inputs.root),
        "--token-source",
        str(inputs.token),
        "--port",
        "65535",
        "--index",
        "8",
    ]


def cli_result(capsys):
    captured = capsys.readouterr()
    assert captured.err == ""
    for private in (TOKEN, NEXT_TOKEN, PRIVATE_ERROR, "private-token-source"):
        assert private not in captured.out
    return json.loads(captured.out)


def test_cli_preview_reads_only_offline_inputs_and_prints_no_raw_values(
    inputs, capsys, monkeypatch
):
    def forbidden(*args, **kwargs):
        pytest.fail("GSI proposal attempted network or process execution")

    before = tree(inputs.root)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert config_cli.main(command(inputs)) == 0
    result = cli_result(capsys)
    assert result == {
        "operation": "preview",
        "isolated_copy_only": True,
        "changed": True,
        "fingerprint": result["fingerprint"],
        "legacy_groups": ["render_before_local_handler", "format_join_open_write"],
        "writes": 1,
        "removals": 0,
    }
    assert len(result["fingerprint"]) == 64
    assert tree(inputs.root) == before
    assert inputs.token.read_bytes() == TOKEN.encode()


@pytest.mark.parametrize(
    "changed",
    ["token", "newline", "identity", "target_identity", "unrelated", "port", "index", "encoding"],
)
def test_cli_digest_binds_token_snapshot_whole_tree_and_explicit_arguments(inputs, capsys, changed):
    argv = command(inputs)
    assert config_cli.main(argv) == 0
    reviewed = cli_result(capsys)["fingerprint"]
    if changed == "token":
        inputs.token.write_bytes(NEXT_TOKEN.encode())
    elif changed == "newline":
        inputs.token.write_bytes(TOKEN.encode() + b"\r\n")
    elif changed in ("identity", "target_identity"):
        selected = inputs.token if changed == "identity" else inputs.cfg / FILENAME
        state = selected.stat()
        replacement = selected.with_suffix(".replacement")
        replacement.write_bytes(selected.read_bytes())
        os.utime(replacement, ns=(state.st_atime_ns, state.st_mtime_ns))
        os.replace(replacement, selected)
    elif changed == "unrelated":
        (inputs.cfg / "nested/unrelated").write_bytes(b"external edit")
    elif changed == "encoding":
        argv.extend(["--encoding", "utf-8-sig"])
    else:
        argv[argv.index("--" + changed) + 1] = "6969" if changed == "port" else "9"
    before = tree(inputs.root)
    assert config_cli.main([*argv, "--apply", "--expect-fingerprint", reviewed]) == 2
    assert cli_result(capsys) == {"error": "config_preview_stale"}
    assert tree(inputs.root) == before


@pytest.mark.parametrize("option", ["--port", "--index"])
def test_module_numeric_errors_do_not_echo_a_pasted_private_token(inputs, option):
    argv = command(inputs)
    argv[argv.index(option) + 1] = TOKEN
    before = tree(inputs.root)
    completed = subprocess.run(
        [sys.executable, "-m", "fsm_panel.config_cli", *argv],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 2 and completed.stderr == ""
    assert json.loads(completed.stdout) == {"error": "config_invalid_arguments"}
    assert TOKEN not in completed.stdout
    assert tree(inputs.root) == before


def test_cli_token_source_cannot_be_the_indexed_output_file(inputs, capsys):
    target = inputs.cfg / FILENAME
    target.write_bytes(TOKEN.encode())
    argv = command(inputs)
    argv[argv.index("--token-source") + 1] = str(target)
    before = tree(inputs.root)
    assert config_cli.main([*argv, "--apply"]) == 2
    assert cli_result(capsys) == {"error": "config_source_destination_overlap"}
    assert tree(inputs.root) == before


def test_cli_structural_token_error_is_content_free(inputs, capsys):
    inputs.token.write_text(TOKEN + '"\n}\n"uri" "https://synthetic.invalid/"')
    before = tree(inputs.root)
    assert config_cli.main([*command(inputs), "--apply"]) == 2
    assert cli_result(capsys) == {"error": "gsi_token_invalid"}
    assert tree(inputs.root) == before


def test_cli_rechecks_source_after_computing_review_fingerprint(inputs, capsys, monkeypatch):
    original = config_cli.preview_fingerprint

    def fingerprint_then_change(proposed):
        result = original(proposed)
        inputs.token.write_bytes(NEXT_TOKEN.encode())
        return result

    monkeypatch.setattr(config_cli, "preview_fingerprint", fingerprint_then_change)
    before = tree(inputs.root)
    assert config_cli.main([*command(inputs), "--apply"]) == 2
    assert cli_result(capsys) == {"error": "config_preview_stale"}
    assert tree(inputs.root) == before


@pytest.mark.parametrize("rollback_denied", [False, True])
def test_cli_failed_install_reports_recovery_without_success_or_token(
    inputs, capsys, monkeypatch, rollback_denied
):
    before = tree(inputs.cfg)
    original = configs._replace_bounded

    def fail_install(source, destination, workspace):
        if source.name.startswith(".config-stage-"):
            raise OSError(PRIVATE_ERROR)
        if rollback_denied and source.name.startswith(".config-backup-"):
            raise OSError(TOKEN)
        return original(source, destination, workspace)

    monkeypatch.setattr(configs, "_replace_bounded", fail_install)
    assert config_cli.main([*command(inputs), "--apply"]) == 2
    result = cli_result(capsys)
    assert result["rollback_succeeded"] is (not rollback_denied)
    assert result["staging_path"] is None
    assert "operation" not in result and "changed" not in result
    if rollback_denied:
        assert result["error"] == "config_rollback_failed"
        assert tree(Path(result["backup_path"])) == before
        assert not inputs.cfg.exists()
    else:
        assert result["error"] == "config_apply_failed"
        assert result["backup_path"] is None
        assert tree(inputs.cfg) == before
