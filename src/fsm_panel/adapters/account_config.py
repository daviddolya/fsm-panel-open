"""Recovered config plans and a separate, explicitly owned offline-copy writer.

Legacy groups describe order and exception scopes, not an executor. Applying a
preview stages an entire account tree and retains its previous tree as backup.
This deliberately changes legacy partial writes and swallowed errors.
"""

import hashlib
import io
import json
import math
import ntpath
import os
import shutil
import stat
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Literal

from fsm_panel.adapters.launch_plan import userdata_id

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TREE_BYTES = 128 * 1024 * 1024
MAX_TREE_ENTRIES = 10000
_MARKER = ".fsm-config-workspace.json"
_LOCK = threading.RLock()
_CONVARS = ("cs2_user_convars_0_slot0.vcfg", "cs2_user_convars_0_slot0.vcfg_lastclouded")


class ConfigError(ValueError):
    """Content-free error code, safe for a user-visible diagnostic."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ConfigStalePreviewError(ConfigError):
    def __init__(self) -> None:
        super().__init__("config_preview_stale")


class ConfigApplyError(OSError):
    def __init__(
        self, *, backup_path: Path | None, staging_path: Path | None, rollback_succeeded: bool
    ) -> None:
        self.code = "config_apply_failed" if rollback_succeeded else "config_rollback_failed"
        self.backup_path = backup_path
        self.staging_path = staging_path
        self.rollback_succeeded = rollback_succeeded
        super().__init__(self.code)


@dataclass(frozen=True)
class LegacyOperation:
    kind: str
    path: str = field(default="", repr=False)
    content: object = field(default=None, repr=False)
    when: str = "always"
    encoding: str = "legacy-default"


@dataclass(frozen=True)
class LegacyGroup:
    """Steps stop at the first exception in their scope unless noted otherwise."""

    name: str
    operations: tuple[LegacyOperation, ...]
    catches: Literal["Exception", "BaseException", "none"]
    on_error: str


@dataclass(frozen=True)
class ConfigTemplates:
    sharedconfig: str = field(repr=False)
    serverbrowser: str = field(repr=False)
    remotecache: str = field(repr=False)
    localconfig: str = field(repr=False)
    friends_block: str = field(repr=False)


def load_config_templates() -> ConfigTemplates:
    """Read only the five generic resources and verify their published hashes."""
    package = files("fsm_panel.config_templates")
    manifest = json.loads(package.joinpath("manifest.json").read_text(encoding="utf-8"))
    contents = {}
    expected = {
        "sharedconfig.vdf.template",
        "serverbrowser_hist.vdf.template",
        "remotecache.vdf.template",
        "localconfig.vdf.template",
        "friends-block.vdf.template",
    }
    if {entry["file"] for entry in manifest} != expected or len(manifest) != len(expected):
        raise ConfigError("config_template_manifest_invalid")
    for entry in manifest:
        raw = package.joinpath(entry["file"]).read_bytes()
        if len(raw) != entry["utf8_bytes"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ConfigError("config_template_hash_mismatch")
        contents[entry["file"]] = raw.decode("utf-8")
    return ConfigTemplates(
        contents["sharedconfig.vdf.template"],
        contents["serverbrowser_hist.vdf.template"],
        contents["remotecache.vdf.template"],
        contents["localconfig.vdf.template"],
        contents["friends-block.vdf.template"],
    )


@dataclass(frozen=True)
class AccountConfigSettings:
    steam_path: str = field(repr=False)
    login: str = field(repr=False)
    force: bool
    disable_steam_overlay: bool
    no_avast_mode: bool
    # Explicit result of qmmconnect_data.get(str(uid)); None differs from ""/False.
    mapped_localconfig: object = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.steam_path) is not str or type(self.login) is not str:
            raise ConfigError("config_string_required")
        if any(
            type(value) is not bool
            for value in (self.force, self.disable_steam_overlay, self.no_avast_mode)
        ):
            raise ConfigError("config_boolean_required")


@dataclass(frozen=True)
class AccountConfigFacts:
    userdata_listable: bool
    account_exists: bool
    cfg_exists: bool
    localconfig_exists: bool


@dataclass(frozen=True)
class ConfigWrite:
    relative_path: str
    content: object = field(repr=False)
    encoding: str = "selected-policy"


@dataclass(frozen=True)
class AccountConfigPlan:
    account_id: str = field(repr=False)
    account_path: str = field(repr=False)
    groups: tuple[LegacyGroup, ...]
    writes: tuple[ConfigWrite, ...]
    removals: tuple[str, ...]
    log_messages: tuple[str, ...] = field(repr=False)
    localconfig_action: Literal["untouched", "preserve", "create", "restore"]
    timestamp: int


def build_account_config_plan(
    settings: AccountConfigSettings,
    *,
    steam_id: int,
    video: str,
    machine: str,
    templates: ConfigTemplates,
    facts: AccountConfigFacts,
    now: int | float,
) -> AccountConfigPlan:
    """Pure recovered success path plus explicit legacy error groups.

    ``mapped_localconfig`` is an already resolved mapping value. Custom mapping
    lookup errors and user log callbacks are not executed. Non-string mapped values
    remain descriptive here; the safe writer rejects them before any destination IO.
    """
    if type(now) not in (int, float) or (type(now) is float and not math.isfinite(now)):
        raise ConfigError("config_finite_time_required")
    if type(video) is not str or type(machine) is not str:
        raise ConfigError("config_string_required")
    uid = str(userdata_id(steam_id))
    steam = "\\".join(settings.steam_path.split("\\")[:-1])
    userdata = steam + "\\userdata"
    account = userdata + "\\" + uid
    cfg = account + "\\730\\local\\cfg"
    remote7 = account + "\\7\\remote"
    localconfig = account + "\\config\\localconfig.vdf"
    timestamp = round(now - 60)
    shared = templates.sharedconfig
    history = templates.serverbrowser % str(timestamp)
    cache = templates.remotecache
    prefix = f"[{settings.login}] "
    logs = []
    if not facts.userdata_listable:
        logs.append("Check your STEAM_PATH!")
    elif not settings.force:
        logs.append(
            prefix
            + (
                "Already has config folder!"
                if facts.account_exists
                else "Config folder was created!"
            )
        )
    if facts.cfg_exists and not settings.force:
        logs.append(prefix + "Settings folder was added!")
    if not settings.force:
        logs.append(prefix + "Setting was updated!")
    op = LegacyOperation
    groups = [
        LegacyGroup(
            "preliminary_listdir",
            (op("listdir", userdata),),
            "Exception",
            "log_check_steam_path_return_from_helper_only",
        ),
        LegacyGroup(
            "preliminary_mkdir",
            (
                op(
                    "makedirs_no_exist_ok",
                    ntpath.join(steam, "userdata", uid),
                    when="userdata_listdir_succeeded",
                ),
                op(
                    "log",
                    content=prefix + "Config folder was created!",
                    when="mkdir_succeeded_and_not_force",
                ),
            ),
            "BaseException",
            "log_already_has_folder_if_not_force; log_failure_propagates",
        ),
        LegacyGroup(
            "read_sources",
            (
                op("read", r"settings\cs2_video.txt"),
                op("read", r"settings\cs2_machine_convars.vcfg"),
            ),
            "none",
            "propagate; preliminary_helper_may_have_created_account",
        ),
        LegacyGroup(
            "remove_remote730",
            (op("rmtree_if_directory_else_remove", account + "\\730\\remote", when="exists"),),
            "Exception",
            "ignore_continue",
        ),
        LegacyGroup(
            "remove_convars",
            tuple(op("remove", cfg + "\\" + name, when="exists") for name in _CONVARS),
            "Exception",
            "skip_remaining_group_continue",
        ),
        LegacyGroup(
            "mkdir_remote7", (op("makedirs_no_exist_ok", remote7),), "Exception", "ignore_continue"
        ),
        LegacyGroup(
            "write_app7",
            (
                op("write", remote7 + "\\sharedconfig.vdf", shared),
                op("write", remote7 + "\\serverbrowser_hist.vdf", history),
                op("write", account + "\\7\\remotecache.vdf", cache),
            ),
            "BaseException",
            "print_traceback_skip_remaining_group_continue",
        ),
        LegacyGroup(
            "prepare_cfg",
            (
                op("makedirs_no_exist_ok", cfg),
                op("truncate_and_close", cfg + "\\cs2_video.txt"),
                op("truncate_and_close", cfg + "\\cs2_machine_convars.vcfg"),
            ),
            "BaseException",
            "log_settings_folder_added_if_not_force; copy_cfg_runs_in_finally_even_if_log_raises",
        ),
        LegacyGroup(
            "copy_cfg_finally",
            (
                op("print", content="Checking CFG!"),
                op("write", cfg + "\\cs2_video.txt", video),
                op("write", cfg + "\\cs2_machine_convars.vcfg", machine),
                op("log", content=prefix + "Setting was updated!", when="not_force"),
            ),
            "Exception",
            "print_traceback_skip_remaining_group; pending_prepare_cfg_exception_propagates",
        ),
    ]
    writes = [
        ConfigWrite("7/remote/sharedconfig.vdf", shared),
        ConfigWrite("7/remote/serverbrowser_hist.vdf", history),
        ConfigWrite("7/remotecache.vdf", cache),
        ConfigWrite("730/local/cfg/cs2_video.txt", video),
        ConfigWrite("730/local/cfg/cs2_machine_convars.vcfg", machine),
    ]
    local_action: Literal["untouched", "preserve", "create", "restore"] = "untouched"
    local_condition = settings.disable_steam_overlay or settings.mapped_localconfig is not None
    local_ops = []
    if local_condition:
        local_ops.append(op("makedirs_no_exist_ok_ignore_Exception", account + "\\config"))
        if facts.localconfig_exists:
            local_ops.append(op("open_read_close_without_reading", localconfig, encoding="utf-8"))
            # The shipped friends-block constant is truthy. No substring test.
            if templates.friends_block:
                local_action = "preserve"
            else:
                local_ops.append(
                    op("write_catch_Exception_print_traceback", localconfig, templates.localconfig)
                )
                writes.append(ConfigWrite("config/localconfig.vdf", templates.localconfig))
                local_action = "create"
        else:
            local_ops.append(
                op("write_catch_Exception_print_traceback", localconfig, templates.localconfig)
            )
            writes.append(ConfigWrite("config/localconfig.vdf", templates.localconfig))
            local_action = "create"
    groups.append(
        LegacyGroup(
            "localconfig_default",
            tuple(local_ops),
            "Exception",
            "print_traceback_continue_to_restore",
        )
    )
    restore_ops: list[LegacyOperation] = []
    if not settings.no_avast_mode and settings.mapped_localconfig is not None:
        restore_ops.extend(
            (
                op("print", content="FOUND EXISTED LOCALCONFIG!!!"),
                op(
                    "write_catch_Exception_print_traceback",
                    localconfig,
                    settings.mapped_localconfig,
                    encoding="utf-8",
                ),
            )
        )
        writes.append(ConfigWrite("config/localconfig.vdf", settings.mapped_localconfig, "utf-8"))
        local_action = "restore"
    groups.append(
        LegacyGroup(
            "localconfig_restore",
            tuple(restore_ops),
            "Exception",
            "ignore_mapping_or_print_error; NO_AVAST_truth_is_outside_catch",
        )
    )
    return AccountConfigPlan(
        uid,
        account,
        tuple(groups),
        tuple(writes),
        ("730/remote", *("730/local/cfg/" + name for name in _CONVARS)),
        tuple(logs),
        local_action,
        timestamp,
    )


@dataclass(frozen=True)
class ConfigWorkspace:
    root: Path
    token: str = field(repr=False)

    @property
    def steam_root(self) -> Path:
        return self.root / "steam"

    @property
    def game_root(self) -> Path:
        return self.root / "game"


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(path))


def _check_path(path: Path, *, destination: bool = False) -> None:
    """Check before resolve, including every existing ancestor and hardlink."""
    for item in (path, *path.parents):
        if item.is_symlink() or item.is_junction():
            raise ConfigError("config_link_rejected")
        if item.exists():
            details = item.lstat()
            if getattr(details, "st_file_attributes", 0) & 0x400:
                raise ConfigError("config_reparse_point_rejected")
            if stat.S_ISREG(details.st_mode) and details.st_nlink != 1:
                raise ConfigError("config_hardlink_rejected")
            if not stat.S_ISREG(details.st_mode) and not stat.S_ISDIR(details.st_mode):
                raise ConfigError("config_special_file_rejected")
        if destination and (item / "Panel.exe").exists():
            raise ConfigError("config_original_distribution_rejected")


def _check_workspace(workspace: ConfigWorkspace) -> None:
    _check_path(workspace.root, destination=True)
    for ancestor in workspace.root.parents:
        if (ancestor / "steam.exe").exists():
            raise ConfigError("config_live_steam_ancestor_rejected")
    marker = workspace.root / _MARKER
    _check_path(marker, destination=True)
    try:
        raw, _ = _read_file(marker)
        value = json.loads(raw)
    except OSError, ValueError, RecursionError:
        raise ConfigError("config_workspace_marker_required") from None
    if value != {"version": 1, "purpose": "offline-config-copy", "token": workspace.token}:
        raise ConfigError("config_workspace_marker_invalid")


def create_config_workspace(new_root: str | Path) -> ConfigWorkspace:
    """Create a NEW root only. Never adopt or mark an existing Steam directory.

    Copy synthetic/offline data into its fixed steam/ and game/ children separately.
    No source directory is imported or executed by this function.
    """
    root = _absolute(new_root)
    _check_path(root, destination=True)
    for ancestor in root.parents:
        if (ancestor / "steam.exe").exists():
            raise ConfigError("config_live_steam_ancestor_rejected")
    if root.exists():
        raise ConfigError("config_new_workspace_required")
    if not root.parent.is_dir():
        raise ConfigError("config_workspace_parent_required")
    workspace = ConfigWorkspace(root, uuid.uuid4().hex)
    root.mkdir()  # No exist_ok: racing creators must fail.
    (root / _MARKER).write_text(
        json.dumps({"version": 1, "purpose": "offline-config-copy", "token": workspace.token}),
        encoding="utf-8",
    )
    (workspace.steam_root / "userdata").mkdir(parents=True)
    workspace.game_root.mkdir()
    return workspace


def open_config_workspace(root: str | Path) -> ConfigWorkspace:
    """Reopen only a workspace bearing a marker created by this adapter."""
    path = _absolute(root)
    _check_path(path, destination=True)
    marker = path / _MARKER
    _check_path(marker, destination=True)
    try:
        raw, _ = _read_file(marker)
        token = json.loads(raw)["token"]
    except OSError, ValueError, KeyError, TypeError, RecursionError:
        raise ConfigError("config_workspace_marker_required") from None
    if (
        type(token) is not str
        or len(token) != 32
        or any(c not in "0123456789abcdef" for c in token)
    ):
        raise ConfigError("config_workspace_marker_invalid")
    workspace = ConfigWorkspace(path, token)
    _check_workspace(workspace)
    return workspace


def _relative(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative).parts
    if not parts or any(part in (".", "..", "") for part in parts) or "\\" in relative:
        raise ConfigError("config_invalid_relative_path")
    for part in parts:
        _validate_filename(part)
    path = root.joinpath(*parts)
    _check_path(path, destination=True)
    if not path.resolve().is_relative_to(root.resolve()) or path == root:
        raise ConfigError("config_path_escape")
    return path


def _validate_filename(name: str) -> None:
    if type(name) is not str or not name or name in (".", ".."):
        raise ConfigError("config_invalid_filename")
    if any(ord(c) < 32 or c in '<>:"/\\|?*' for c in name) or name.endswith((" ", ".")):
        raise ConfigError("config_invalid_filename")
    if ntpath.isreserved(name):
        raise ConfigError("config_invalid_filename")


@dataclass(frozen=True)
class _Stat:
    device: int
    inode: int
    size: int
    modified_ns: int
    mode: int

    @classmethod
    def capture(cls, details: os.stat_result) -> _Stat:
        return cls(
            details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns, details.st_mode
        )


def _read_file(path: Path) -> tuple[bytes, _Stat]:
    _check_path(path)
    with path.open("rb") as handle:
        before = _Stat.capture(os.fstat(handle.fileno()))
        if not stat.S_ISREG(before.mode):
            raise ConfigError("config_regular_file_required")
        if before.size > MAX_FILE_BYTES:
            raise ConfigError("config_file_too_large")
        raw = handle.read(MAX_FILE_BYTES + 1)
        after = _Stat.capture(os.fstat(handle.fileno()))
    if len(raw) > MAX_FILE_BYTES:
        raise ConfigError("config_file_too_large")
    _check_path(path)
    if before != after or after != _Stat.capture(path.stat()):
        raise ConfigStalePreviewError()
    return raw, after


def _decode(raw: bytes, encoding: str) -> str:
    # Explicit new encoding policy; universal newline conversion matches text reads.
    try:
        return io.TextIOWrapper(io.BytesIO(raw), encoding=encoding, newline=None).read()
    except LookupError, UnicodeError:
        raise ConfigError("config_decode_failed") from None


def _encode(text: object, encoding: str) -> bytes:
    if type(text) is not str:
        raise ConfigError("config_mapped_string_required")
    try:
        # Deterministic Windows text-write policy even on another OS.
        raw = text.replace("\n", "\r\n").encode(encoding)
    except LookupError, UnicodeError:
        raise ConfigError("config_encode_failed") from None
    if len(raw) > MAX_FILE_BYTES:
        raise ConfigError("config_file_too_large")
    return raw


@dataclass(frozen=True)
class _Source:
    path: Path = field(repr=False)
    state: _Stat
    sha256: str


SourceSnapshot = _Source


@dataclass(frozen=True)
class _Entry:
    relative_path: str = field(repr=False)
    state: _Stat
    sha256: str | None


@dataclass(frozen=True)
class _Tree:
    exists: bool
    entries: tuple[_Entry, ...]


def _snapshot_tree(path: Path) -> _Tree:
    _check_path(path, destination=True)
    if not path.exists():
        return _Tree(False, ())
    if not path.is_dir():
        raise ConfigError("config_directory_required")
    entries: list[_Entry] = []
    size = 0
    pending = [path]
    while pending:
        item = pending.pop()
        _check_path(item, destination=True)
        if item.is_dir():
            state = _Stat.capture(item.stat())
            digest = None
            children = []
            for child in item.iterdir():
                children.append(child)
                if len(entries) + len(pending) + len(children) + 1 > MAX_TREE_ENTRIES:
                    raise ConfigError("config_tree_too_large")
            pending.extend(sorted(children, reverse=True))
        else:
            raw, state = _read_file(item)
            size += len(raw)
            digest = hashlib.sha256(raw).hexdigest()
        entries.append(_Entry(item.relative_to(path).as_posix(), state, digest))
        if len(entries) > MAX_TREE_ENTRIES or size > MAX_TREE_BYTES:
            raise ConfigError("config_tree_too_large")
    return _Tree(True, tuple(entries))


def _source(path: Path, workspace: ConfigWorkspace, encoding: str) -> tuple[str, _Source]:
    _check_path(path)
    if path.resolve().is_relative_to(workspace.root.resolve()):
        raise ConfigError("config_source_destination_overlap")
    raw, state = _read_file(path)
    return _decode(raw, encoding), _Source(path, state, hashlib.sha256(raw).hexdigest())


def read_config_input(
    path: str | Path, workspace: ConfigWorkspace, encoding: str = "utf-8"
) -> tuple[str, SourceSnapshot]:
    """Read an auxiliary input with the same copy boundary as game templates.

    The returned snapshot tracks raw bytes and identity even when newline
    normalization produces identical text. Re-read and compare it before apply.
    """
    _check_workspace(workspace)
    return _source(_absolute(path), workspace, encoding)


def _verify_auxiliary(workspace: ConfigWorkspace, snapshots: tuple[SourceSnapshot, ...]) -> None:
    for snapshot in snapshots:
        _check_path(snapshot.path)
        if snapshot.path.resolve().is_relative_to(workspace.root.resolve()):
            raise ConfigError("config_source_destination_overlap")
        raw, state = _read_file(snapshot.path)
        if state != snapshot.state or hashlib.sha256(raw).hexdigest() != snapshot.sha256:
            raise ConfigStalePreviewError()


@dataclass(frozen=True)
class _Transaction:
    workspace: ConfigWorkspace
    target_relative: str
    before: _Tree = field(repr=False)
    writes: tuple[tuple[str, bytes], ...] = field(repr=False)
    removals: tuple[str, ...]


@dataclass(frozen=True)
class AccountConfigPreview:
    workspace: ConfigWorkspace
    settings: AccountConfigSettings
    steam_id: int = field(repr=False)
    video_source: Path = field(repr=False)
    machine_source: Path = field(repr=False)
    now: int | float
    encoding: str
    plan: AccountConfigPlan
    sources: tuple[_Source, ...] = field(repr=False)
    transaction: _Transaction = field(repr=False)
    auxiliary_sources: tuple[SourceSnapshot, ...] = field(default=(), repr=False)

    @property
    def target_path(self) -> Path:
        return self.workspace.root / self.transaction.target_relative


@dataclass(frozen=True)
class ConfigApplyResult:
    changed: bool
    target_path: Path
    backup_path: Path | None


def preview_account_config(
    workspace: ConfigWorkspace,
    settings: AccountConfigSettings,
    *,
    steam_id: int,
    video_source: str | Path,
    machine_source: str | Path,
    now: int | float,
    encoding: str = "utf-8",
    auxiliary_sources: tuple[SourceSnapshot, ...] = (),
) -> AccountConfigPreview:
    """Read explicit source files and the isolated account tree; write nothing."""
    _check_workspace(workspace)
    _verify_auxiliary(workspace, auxiliary_sources)
    uid = str(userdata_id(steam_id))
    _validate_filename(uid)
    relative = "steam/userdata/" + uid
    target = _relative(workspace.root, relative)
    before = _snapshot_tree(target)
    video_path, machine_path = _absolute(video_source), _absolute(machine_source)
    video, video_state = _source(video_path, workspace, encoding)
    machine, machine_state = _source(machine_path, workspace, encoding)
    if video_path.samefile(machine_path):
        raise ConfigError("config_sources_must_differ")
    facts = AccountConfigFacts(
        target.parent.is_dir(),
        target.exists(),
        (target / "730/local/cfg").exists(),
        (target / "config/localconfig.vdf").exists(),
    )
    plan = build_account_config_plan(
        settings,
        steam_id=steam_id,
        video=video,
        machine=machine,
        templates=load_config_templates(),
        facts=facts,
        now=now,
    )
    writes = tuple(
        (
            write.relative_path,
            _encode(
                write.content, encoding if write.encoding == "selected-policy" else write.encoding
            ),
        )
        for write in plan.writes
    )
    transaction = _Transaction(workspace, relative, before, writes, plan.removals)
    # Tree/source races during collection are detected before exposing a preview.
    if _snapshot_tree(target) != before:
        raise ConfigStalePreviewError()
    return AccountConfigPreview(
        workspace,
        settings,
        steam_id,
        video_path,
        machine_path,
        now,
        encoding,
        plan,
        (video_state, machine_state),
        transaction,
        auxiliary_sources,
    )


def _verify_account(preview: AccountConfigPreview) -> None:
    try:
        current = preview_account_config(
            preview.workspace,
            preview.settings,
            steam_id=preview.steam_id,
            video_source=preview.video_source,
            machine_source=preview.machine_source,
            now=preview.now,
            encoding=preview.encoding,
            auxiliary_sources=preview.auxiliary_sources,
        )
    except ConfigError, OSError, ValueError:
        raise ConfigStalePreviewError() from None
    if current != preview:
        raise ConfigStalePreviewError()


def _remove_bounded(path: Path, root: Path) -> None:
    _check_path(root, destination=True)
    _check_path(path, destination=True)
    if not path.resolve().is_relative_to(root.resolve()) or path == root:
        raise ConfigError("config_path_escape")
    if path.is_dir():
        _snapshot_tree(path)  # Reject links/hardlinks throughout before recursion.
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _discard_stage(stage: Path, root: Path) -> None:
    # Cleanup cannot mask an apply failure or its retained backup metadata.
    try:
        _remove_bounded(stage, root)
    except OSError, ConfigError:
        pass


def _replace_bounded(source: Path, destination: Path, workspace: ConfigWorkspace) -> None:
    _check_workspace(workspace)
    for path in (source, destination):
        _check_path(path, destination=True)
        if path == workspace.root or not path.resolve().is_relative_to(workspace.root.resolve()):
            raise ConfigError("config_path_escape")
        if path.exists():
            _snapshot_tree(path)
    if destination.exists():
        raise FileExistsError("config_rename_destination_exists")
    os.replace(source, destination)


def _commit_transaction(transaction: _Transaction, verify: Callable[[], None]) -> ConfigApplyResult:
    workspace = transaction.workspace
    target = _relative(workspace.root, transaction.target_relative)
    stage = workspace.root / (".config-stage-" + uuid.uuid4().hex)
    backup: Path | None = None
    moved_original = False
    stage_created = False
    failure: ConfigApplyError | None = None
    try:
        _check_workspace(workspace)
        _check_path(stage, destination=True)
        stage.mkdir()  # Reserve before copying; never clean another attempt's tree.
        stage_created = True
        if transaction.before.exists:
            if _snapshot_tree(target) != transaction.before:
                raise ConfigStalePreviewError()
            # Snapshot rejects every link before copytree; do not follow links.
            shutil.copytree(target, stage, symlinks=True, dirs_exist_ok=True)
            copied = _snapshot_tree(stage)
            if tuple((e.relative_path, e.sha256) for e in copied.entries) != tuple(
                (e.relative_path, e.sha256) for e in transaction.before.entries
            ):
                raise ConfigStalePreviewError()
        for relative in transaction.removals:
            removal = _relative(stage, relative)
            if removal.is_dir() and relative != "730/remote":
                raise ConfigError("config_file_removal_required")
            _remove_bounded(removal, stage)
        for relative, raw in transaction.writes:
            path = _relative(stage, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            _check_path(path, destination=True)
            with path.open("wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        _snapshot_tree(stage)
        verify()
        if transaction.before.exists:
            backup = workspace.root / (".config-backup-" + uuid.uuid4().hex)
            _replace_bounded(target, backup, workspace)
            moved_original = True
        _replace_bounded(stage, target, workspace)
    except BaseException as error:
        if backup is not None and backup.exists() and not target.exists():
            moved_original = True
        if isinstance(error, ConfigStalePreviewError) and not moved_original:
            raise
        rollback = not moved_original
        if moved_original and backup is not None:
            try:
                # Never clobber an independently created replacement.
                if not target.exists():
                    _replace_bounded(backup, target, workspace)
                    backup = None
                    rollback = True
            except OSError, ConfigError:
                rollback = False
        failure = ConfigApplyError(
            backup_path=backup if moved_original else None,
            staging_path=None,
            rollback_succeeded=rollback,
        )
        if not isinstance(error, Exception):
            # Restore first, but never turn interrupts into an ordinary result.
            raise
    finally:
        if stage_created:
            _discard_stage(stage, workspace.root)
    if failure is not None:
        failure.staging_path = stage if stage_created and stage.exists() else None
        raise failure from None
    return ConfigApplyResult(True, target, backup)


def apply_account_config(preview: AccountConfigPreview) -> ConfigApplyResult:
    """Revalidate, stage, back up and replace only the owned account directory."""
    with _LOCK:
        _verify_account(preview)
        return _commit_transaction(preview.transaction, lambda: _verify_account(preview))
