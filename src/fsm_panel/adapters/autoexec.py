"""Pure autoexec synchronization contract and a guarded offline-copy adapter."""

import ntpath
from dataclasses import dataclass, field
from pathlib import Path

from fsm_panel.adapters.account_config import (
    _LOCK,
    ConfigApplyResult,
    ConfigError,
    ConfigStalePreviewError,
    ConfigWorkspace,
    LegacyGroup,
    LegacyOperation,
    _absolute,
    _check_workspace,
    _commit_transaction,
    _decode,
    _encode,
    _read_file,
    _relative,
    _snapshot_tree,
    _Source,
    _source,
    _Transaction,
    _validate_filename,
)


@dataclass(frozen=True)
class AutoexecPlan:
    cfg_path: str = field(repr=False)
    config_file_name: str
    changed: bool
    groups: tuple[LegacyGroup, ...]
    content: str = field(repr=False)
    # Legacy outer handler returns False, while safe apply raises ConfigApplyError.
    on_outer_exception: str = "print_HANDELED_and_traceback_return_False"


def build_autoexec_plan(
    *,
    csgo_path: str,
    config_file_name: str,
    source_content: str,
    existing_content: str | None,
) -> AutoexecPlan:
    """Pure decoded-string comparison. None represents a failed/missing old read.

    The name is always explicit: its global initializer was outside the recovered
    helper. This descriptive plan uses ntpath.join without filesystem normalization.
    The safe adapter separately rejects names that could escape its fixture root.
    """
    if any(type(value) is not str for value in (csgo_path, config_file_name, source_content)):
        raise ConfigError("config_string_required")
    if existing_content is not None and type(existing_content) is not str:
        raise ConfigError("config_string_required")
    cfg = ntpath.join(csgo_path, r"game\csgo\cfg")
    autoexec = ntpath.join(cfg, "autoexec.cfg")
    op = LegacyOperation
    groups = [
        LegacyGroup(
            "read_source",
            (op("read", "settings\\" + format(config_file_name, "")),),
            "none",
            "outer_Exception_print_and_return_False",
        ),
    ]
    if source_content == existing_content:
        groups.append(
            LegacyGroup(
                "read_compare_existing",
                (op("read", autoexec), op("return_True_before_any_removal")),
                "Exception",
                "ignore_continue",
            )
        )
        return AutoexecPlan(cfg, config_file_name, False, tuple(groups), source_content)
    groups.extend(
        (
            LegacyGroup(
                "read_compare_existing",
                (
                    op("read", autoexec),
                    op("remove", autoexec, when="read_succeeded_and_content_differs"),
                ),
                "Exception",
                "FileNotFoundError_and_Exception_ignored_continue",
            ),
            LegacyGroup(
                "remove_legacy_name",
                (op("remove", ntpath.join(cfg, format(config_file_name, ""))),),
                "Exception",
                "ignore_continue",
            ),
            LegacyGroup(
                "write_autoexec",
                (op("write_and_close", autoexec, source_content), op("return_True")),
                "none",
                "outer_Exception_print_and_return_False; no_rollback",
            ),
        )
    )
    return AutoexecPlan(cfg, config_file_name, True, tuple(groups), source_content)


@dataclass(frozen=True)
class AutoexecPreview:
    workspace: ConfigWorkspace
    source_path: Path = field(repr=False)
    config_file_name: str
    encoding: str
    plan: AutoexecPlan
    source: _Source = field(repr=False)
    transaction: _Transaction = field(repr=False)

    @property
    def target_path(self) -> Path:
        return self.workspace.root / self.transaction.target_relative / "autoexec.cfg"


def preview_autoexec(
    workspace: ConfigWorkspace,
    *,
    source_path: str | Path,
    config_file_name: str,
    encoding: str = "utf-8",
) -> AutoexecPreview:
    """Preview only game/game/csgo/cfg in an explicitly owned workspace.

    Like the legacy helper, this never creates a missing cfg directory. Source
    decoding occurs before destination reads. Destination read failures are modeled
    by the pure plan but the guarded adapter refuses unreadable snapshots.
    """
    _check_workspace(workspace)
    _validate_filename(config_file_name)
    source = _absolute(source_path)
    content, source_state = _source(source, workspace, encoding)
    relative = "game/game/csgo/cfg"
    target = _relative(workspace.root, relative)
    before = _snapshot_tree(target)
    if not before.exists:
        raise ConfigError("config_autoexec_directory_missing")
    autoexec = target / "autoexec.cfg"
    existing = _decode(_read_file(autoexec)[0], encoding) if autoexec.exists() else None
    plan = build_autoexec_plan(
        csgo_path=str(workspace.game_root),
        config_file_name=config_file_name,
        source_content=content,
        existing_content=existing,
    )
    # Equality must preserve BOTH autoexec and the file named by CONFIG_FILE_NAME.
    removals = ("autoexec.cfg", config_file_name) if plan.changed else ()
    writes = (("autoexec.cfg", _encode(content, encoding)),) if plan.changed else ()
    transaction = _Transaction(workspace, relative, before, writes, removals)
    if _snapshot_tree(target) != before:
        raise ConfigStalePreviewError()
    return AutoexecPreview(
        workspace, source, config_file_name, encoding, plan, source_state, transaction
    )


def _verify_autoexec(preview: AutoexecPreview) -> None:
    try:
        current = preview_autoexec(
            preview.workspace,
            source_path=preview.source_path,
            config_file_name=preview.config_file_name,
            encoding=preview.encoding,
        )
    except ConfigError, OSError, ValueError:
        raise ConfigStalePreviewError() from None
    if current != preview:
        raise ConfigStalePreviewError()


def apply_autoexec(preview: AutoexecPreview) -> ConfigApplyResult:
    """Stage the whole cfg copy and retain its previous tree as a backup."""
    with _LOCK:
        _verify_autoexec(preview)
        if not preview.plan.changed:
            return ConfigApplyResult(False, preview.target_path, None)
        result = _commit_transaction(preview.transaction, lambda: _verify_autoexec(preview))
        return ConfigApplyResult(result.changed, preview.target_path, result.backup_path)
