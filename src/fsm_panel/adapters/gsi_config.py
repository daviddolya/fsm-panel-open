"""Recovered GSI file/probe plans and an explicit offline-workspace writer.

Only the legacy functions describe original string/path behavior. Authentication,
validation, deterministic encoding and backed-up transactions are new safeguards.
No function probes a port, starts a listener or edits a live game installation.
"""

import hashlib
import math
import ntpath
from collections.abc import Sequence
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path

from fsm_panel.adapters.account_config import (
    _LOCK,
    ConfigApplyResult,
    ConfigError,
    ConfigStalePreviewError,
    ConfigWorkspace,
    LegacyGroup,
    LegacyOperation,
    SourceSnapshot,
    _absolute,
    _check_path,
    _check_workspace,
    _commit_transaction,
    _encode,
    _read_file,
    _relative,
    _snapshot_tree,
    _Transaction,
)

type LegacyScalar = None | bool | int | float | str

GSI_TEMPLATE_SHA256 = "6b02e3f2bdc3afef44437795cb36ea5c1096e1ec980b91f3d55ab2e40f84ed8f"
_TEMPLATE_BYTES = 1268
_CFG_RELATIVE = "game/game/csgo/cfg"


def load_gsi_template() -> str:
    """Load only the published sanitized resource, with a fixed identity check."""
    try:
        raw = files("fsm_panel.gsi_templates").joinpath("gsi.sanitized.template").read_bytes()
    except OSError, ModuleNotFoundError:
        raise ConfigError("gsi_template_unavailable") from None
    if len(raw) != _TEMPLATE_BYTES or hashlib.sha256(raw).hexdigest() != GSI_TEMPLATE_SHA256:
        raise ConfigError("gsi_template_hash_mismatch")
    return raw.decode("utf-8")


def _legacy_scalar(value: LegacyScalar) -> None:
    if type(value) not in (str, int, float, bool, type(None)) or (
        type(value) is float and not math.isfinite(value)
    ):
        raise ConfigError("gsi_legacy_scalar_required")


def render_legacy_gsi_content(template: str, running_on_port: LegacyScalar) -> str:
    """Original all-occurrence str.replace, with a new inert-input boundary.

    TOKEN_PLACEHOLDER remains untouched. This function is descriptive and can
    produce an invalid URI; it does not validate port ranges or execute writes.
    """
    if type(template) is not str:
        raise ConfigError("gsi_template_string_required")
    _legacy_scalar(running_on_port)
    return template.replace("PORT_PLACEHOLDER", str(running_on_port))


def legacy_gsi_config_path(csgo_path: str, gsi_file_index: LegacyScalar) -> str:
    """Original Windows join and empty-spec index formatting, without normalization."""
    if type(csgo_path) is not str:
        raise ConfigError("gsi_path_string_required")
    _legacy_scalar(gsi_file_index)
    filename = "gamestate_integration_" + format(gsi_file_index, "") + ".cfg"
    return ntpath.join(csgo_path, r"game\csgo\cfg", filename)


@dataclass(frozen=True)
class GsiConfigPlan:
    target_path: str = field(repr=False)
    content: str = field(repr=False)
    groups: tuple[LegacyGroup, ...]
    always_rewrite: bool = True
    creates_directory: bool = False
    deletes_old_indices: bool = False


def _plan(target_path: str, content: str) -> GsiConfigPlan:
    op = LegacyOperation
    return GsiConfigPlan(
        target_path,
        content,
        (
            LegacyGroup(
                "render_before_local_handler",
                (op("str_running_on_port"), op("replace_all_PORT_PLACEHOLDER")),
                "none",
                "outer_launch_error_path",
            ),
            LegacyGroup(
                "format_join_open_write",
                (
                    op("format_index_empty_spec"),
                    op("join_filename", target_path),
                    op("open_context_mode_w", target_path),
                    op("write", target_path, content),
                ),
                "Exception",
                "log_Wrong_CS2_path_then_continue; log_failure_can_propagate",
            ),
        ),
    )


def build_legacy_gsi_plan(
    *,
    csgo_path: str,
    running_on_port: LegacyScalar,
    gsi_file_index: LegacyScalar,
    template: str,
) -> GsiConfigPlan:
    """Describe the recovered write and exception scopes using supplied safe data."""
    content = render_legacy_gsi_content(template, running_on_port)
    target = legacy_gsi_config_path(csgo_path, gsi_file_index)
    return _plan(target, content)


@dataclass(frozen=True)
class LegacyGsiSelection:
    running_on_port: int
    gsi_file_index: int
    probes_consumed: int
    all_probes_completed: bool


def select_legacy_gsi_port(probe_outcomes: Sequence[bool]) -> LegacyGsiSelection:
    """Select from supplied complete-expression outcomes; never allocate a port.

    True means GET/status/format/print completed, independent of status code.
    False means an ordinary Exception occurred anywhere in that expression.
    Only the prefix the original loop would consume is inspected.
    """
    if type(probe_outcomes) not in (list, tuple):
        raise ConfigError("gsi_probe_results_required")
    for offset, candidate in enumerate(range(6969, 6979)):
        if offset >= len(probe_outcomes):
            raise ConfigError("gsi_probe_results_incomplete")
        outcome = probe_outcomes[offset]
        if type(outcome) is not bool:
            raise ConfigError("gsi_probe_boolean_required")
        if not outcome:
            return LegacyGsiSelection(candidate, candidate - 6969 + 1, offset + 1, False)
    return LegacyGsiSelection(6969, 1, 10, True)


def _port(value: int) -> None:
    if type(value) is not int or not 1 <= value <= 65535:
        raise ConfigError("gsi_port_invalid")


def _index(value: int) -> None:
    if type(value) is not int or not 1 <= value <= 10:
        raise ConfigError("gsi_index_invalid")


def _token(value: str) -> None:
    # A stricter subset of the receiver's 32..256 printable-ASCII token contract.
    # URL-safe base64 characters cannot change the quoted VDF value's structure.
    if (
        type(value) is not str
        or not 32 <= len(value) <= 256
        or any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in value
        )
    ):
        raise ConfigError("gsi_token_invalid")


def render_gsi_config(running_on_port: int, token: str) -> str:
    """Render the fixed loopback URI and a caller-supplied fresh VDF-safe token.

    Generate the token with secrets.token_urlsafe(32). This function checks the
    format, not entropy; it neither generates a token nor starts a listener.
    """
    _port(running_on_port)
    _token(token)
    content = render_legacy_gsi_content(load_gsi_template(), running_on_port)
    return content.replace("TOKEN_PLACEHOLDER", token)


@dataclass(frozen=True)
class GsiConfigPreview:
    workspace: ConfigWorkspace
    running_on_port: int
    gsi_file_index: int
    token_source: Path = field(repr=False)
    encoding: str
    plan: GsiConfigPlan
    source: SourceSnapshot = field(repr=False)
    transaction: _Transaction = field(repr=False)

    @property
    def target_path(self) -> Path:
        return (
            self.workspace.root / _CFG_RELATIVE / f"gamestate_integration_{self.gsi_file_index}.cfg"
        )


def _read_token_source(
    source: Path, workspace: ConfigWorkspace, encoding: str
) -> tuple[str, SourceSnapshot]:
    _check_path(source)
    if source.resolve().is_relative_to(workspace.root.resolve()):
        raise ConfigError("config_source_destination_overlap")
    raw, state = _read_file(source)
    try:
        token = raw.decode(encoding)
    except LookupError, UnicodeError:
        raise ConfigError("config_decode_failed") from None
    if token.endswith("\r\n"):
        token = token[:-2]
    elif token.endswith("\n"):
        token = token[:-1]
    _token(token)
    return token, SourceSnapshot(source, state, hashlib.sha256(raw).hexdigest())


def preview_gsi_config(
    workspace: ConfigWorkspace,
    *,
    running_on_port: int,
    gsi_file_index: int,
    token_source: str | Path,
    encoding: str = "utf-8",
) -> GsiConfigPreview:
    """Read token input and a fixed offline cfg tree; never write or probe.

    The index is explicit, independent of the port. The missing cfg directory is
    never created. Equal output is still a write, matching the recovered slice.
    """
    _port(running_on_port)
    _index(gsi_file_index)
    _check_workspace(workspace)
    source_path = _absolute(token_source)
    token, source_state = _read_token_source(source_path, workspace, encoding)
    content = render_gsi_config(running_on_port=running_on_port, token=token)
    target = _relative(workspace.root, _CFG_RELATIVE)
    before = _snapshot_tree(target)
    if not before.exists:
        raise ConfigError("gsi_config_directory_missing")
    filename = f"gamestate_integration_{gsi_file_index}.cfg"
    plan = _plan(legacy_gsi_config_path(str(workspace.game_root), gsi_file_index), content)
    transaction = _Transaction(
        workspace, _CFG_RELATIVE, before, ((filename, _encode(content, encoding)),), ()
    )
    if _snapshot_tree(target) != before:
        raise ConfigStalePreviewError()
    # Catch token changes during collection without exposing its bytes or path.
    if _read_token_source(source_path, workspace, encoding)[1] != source_state:
        raise ConfigStalePreviewError()
    return GsiConfigPreview(
        workspace,
        running_on_port,
        gsi_file_index,
        source_path,
        encoding,
        plan,
        source_state,
        transaction,
    )


def _verify_gsi(preview: GsiConfigPreview) -> None:
    try:
        current = preview_gsi_config(
            preview.workspace,
            running_on_port=preview.running_on_port,
            gsi_file_index=preview.gsi_file_index,
            token_source=preview.token_source,
            encoding=preview.encoding,
        )
    except ConfigError, OSError, ValueError:
        raise ConfigStalePreviewError() from None
    if current != preview:
        raise ConfigStalePreviewError()


def apply_gsi_config(preview: GsiConfigPreview) -> ConfigApplyResult:
    """Always rewrite the indexed file, retaining the full previous cfg tree."""
    with _LOCK:
        _verify_gsi(preview)
        result = _commit_transaction(preview.transaction, lambda: _verify_gsi(preview))
        return ConfigApplyResult(result.changed, preview.target_path, result.backup_path)
