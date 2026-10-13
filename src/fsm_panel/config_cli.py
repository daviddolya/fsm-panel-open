"""Prepare game configuration in an explicitly owned offline workspace."""

import argparse
import hashlib
import json
from collections.abc import Sequence
from dataclasses import fields, is_dataclass
from pathlib import Path

from fsm_panel.adapters.account_config import (
    AccountConfigPreview,
    AccountConfigSettings,
    ConfigApplyError,
    ConfigError,
    ConfigStalePreviewError,
    apply_account_config,
    create_config_workspace,
    open_config_workspace,
    preview_account_config,
)
from fsm_panel.adapters.autoexec import AutoexecPreview, apply_autoexec, preview_autoexec
from fsm_panel.core import SettingsStore
from fsm_panel.core.models import ValidationError

MAX_INPUT_BYTES = 2 * 1024 * 1024


def preview_fingerprint(value: object) -> str:
    """Versioned digest of all proposal fields, including hidden source snapshots.

    Only the digest is displayed; config contents and account metadata are not.
    This is a review correlation token, not authorization or a signature.
    """
    digest = hashlib.sha256(b"fsm-config-preview-v1")

    def piece(tag: str, raw: bytes) -> None:
        digest.update(tag.encode("ascii") + str(len(raw)).encode("ascii") + b":" + raw)

    def visit(item: object) -> None:
        if is_dataclass(item) and not isinstance(item, type):
            piece("class", type(item).__qualname__.encode("utf-8"))
            for field in fields(item):
                piece("field", field.name.encode("utf-8"))
                visit(getattr(item, field.name))
        elif isinstance(item, Path):
            piece("path", str(item).encode("utf-8"))
        elif isinstance(item, tuple | list):
            piece("sequence", str(len(item)).encode("ascii"))
            for child in item:
                visit(child)
        elif type(item) is bytes:
            piece("bytes", item)
        elif item is None or type(item) in (str, bool, int, float):
            piece(type(item).__name__, json.dumps(item, ensure_ascii=True).encode("ascii"))
        else:
            raise ConfigError("config_unhashable_preview")

    visit(value)
    return digest.hexdigest()


def _input(path: Path) -> bytes:
    with path.open("rb") as source:
        raw = source.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ConfigError("config_cli_input_too_large")
    return raw


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("workspace", help="Create a new isolated workspace")
    create.add_argument("path", type=Path)
    account = commands.add_parser("account", help="Prepare one copied account tree")
    account.add_argument("--settings", type=Path, required=True)
    account.add_argument("--steam-id", type=int, required=True)
    account.add_argument("--login", required=True)
    account.add_argument("--video-source", type=Path, required=True)
    account.add_argument("--machine-source", type=Path, required=True)
    account.add_argument("--timestamp", type=float, required=True, help="Explicit Unix time")
    account.add_argument("--localconfig-copy", type=Path)
    account.add_argument("--force", action="store_true", help="Legacy log suppression only")
    autoexec = commands.add_parser("autoexec", help="Synchronize only the copied game cfg tree")
    autoexec.add_argument("--source", type=Path, required=True)
    autoexec.add_argument("--config-file-name", required=True, help="Explicit recovered input")
    for command in (account, autoexec):
        command.add_argument("--workspace", type=Path, required=True)
        command.add_argument("--encoding", default="utf-8")
        command.add_argument("--apply", action="store_true")
        command.add_argument("--expect-fingerprint", help="Digest from the reviewed preview")
    args = parser.parse_args(argv)
    result: dict[str, object]
    try:
        if args.command == "workspace":
            create_config_workspace(args.path)
            result = {"operation": "workspace_created", "isolated_copy_only": True}
        else:
            workspace = open_config_workspace(args.workspace)
            input_snapshots = []
            preview: AccountConfigPreview | AutoexecPreview
            if args.command == "account":
                settings_raw = _input(args.settings)
                settings = SettingsStore.loads(settings_raw)
                input_snapshots.append((args.settings, settings_raw))
                mapped = None
                if args.localconfig_copy:
                    raw = _input(args.localconfig_copy)
                    mapped = raw.decode(args.encoding)
                    input_snapshots.append((args.localconfig_copy, raw))
                preview = preview_account_config(
                    workspace,
                    AccountConfigSettings(
                        steam_path=settings.get("STEAM_PATH"),
                        login=args.login,
                        force=args.force,
                        disable_steam_overlay=settings.get("DISABLE_STEAM_OVERLAY"),
                        no_avast_mode=settings.get("NO_AVAST_MODE"),
                        mapped_localconfig=mapped,
                    ),
                    steam_id=args.steam_id,
                    video_source=args.video_source,
                    machine_source=args.machine_source,
                    now=args.timestamp,
                    encoding=args.encoding,
                )
                changed = True  # Legacy writer rewrites even equal account configs.
            else:
                preview = preview_autoexec(
                    workspace,
                    source_path=args.source,
                    config_file_name=args.config_file_name,
                    encoding=args.encoding,
                )
                changed = preview.plan.changed
            fingerprint = preview_fingerprint(preview)
            if args.expect_fingerprint is not None and args.expect_fingerprint != fingerprint:
                raise ConfigStalePreviewError()
            result = {
                "operation": "apply" if args.apply else "preview",
                "isolated_copy_only": True,
                "changed": changed,
                "fingerprint": fingerprint,
                "legacy_groups": [group.name for group in preview.plan.groups],
                "writes": len(preview.transaction.writes),
                "removals": len(preview.transaction.removals),
            }
            if args.apply:
                for path, raw in input_snapshots:
                    if _input(path) != raw:
                        raise ConfigStalePreviewError()
                applied = (
                    apply_account_config(preview)
                    if isinstance(preview, AccountConfigPreview)
                    else apply_autoexec(preview)
                )
                result["changed"] = applied.changed
                result["backup_path"] = str(applied.backup_path) if applied.backup_path else None
    except ConfigApplyError as error:
        result = {
            "error": error.code,
            "backup_path": str(error.backup_path) if error.backup_path else None,
            "staging_path": str(error.staging_path) if error.staging_path else None,
            "rollback_succeeded": error.rollback_succeeded,
        }
    except ConfigError as error:
        result = {"error": error.code}
    except ValidationError:
        result = {"error": "config_invalid_settings"}
    except LookupError, UnicodeError, ValueError:
        result = {"error": "config_invalid_input"}
    except OSError:
        result = {"error": "config_file_unavailable"}
    print(json.dumps(result, ensure_ascii=False))
    return 2 if "error" in result else 0


if __name__ == "__main__":
    raise SystemExit(main())
