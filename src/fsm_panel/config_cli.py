"""Prepare game configuration in an explicitly owned offline workspace."""

import argparse
import hashlib
import json
from collections.abc import Sequence
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Never

from fsm_panel.adapters.account_config import (
    AccountConfigPreview,
    AccountConfigSettings,
    ConfigApplyError,
    ConfigError,
    ConfigStalePreviewError,
    ConfigWorkspace,
    SourceSnapshot,
    apply_account_config,
    create_config_workspace,
    open_config_workspace,
    preview_account_config,
    read_config_input,
)
from fsm_panel.adapters.autoexec import AutoexecPreview, apply_autoexec, preview_autoexec
from fsm_panel.adapters.gsi_config import GsiConfigPreview, apply_gsi_config, preview_gsi_config
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


def _input(
    path: Path, workspace: ConfigWorkspace, encoding: str = "utf-8-sig"
) -> tuple[str, SourceSnapshot]:
    text, snapshot = read_config_input(path, workspace, encoding)
    if snapshot.state.size > MAX_INPUT_BYTES:
        raise ConfigError("config_cli_input_too_large")
    return text, snapshot


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        # argparse normally echoes malformed values, which may contain a pasted
        # secret. Keep diagnostics stable, including subparser conversion errors.
        raise ConfigError("config_invalid_arguments")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
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
    gsi = commands.add_parser("gsi", help="Prepare an indexed GSI config in the copied cfg tree")
    gsi.add_argument("--token-source", type=Path, required=True, help="Private new token file")
    gsi.add_argument("--port", type=int, required=True)
    gsi.add_argument("--index", type=int, required=True)
    for command in (account, autoexec, gsi):
        command.add_argument("--workspace", type=Path, required=True)
        command.add_argument("--encoding", default="utf-8")
        command.add_argument("--apply", action="store_true")
        command.add_argument("--expect-fingerprint", help="Digest from the reviewed preview")
    result: dict[str, object]
    try:
        args = parser.parse_args(argv)
        if args.command == "workspace":
            create_config_workspace(args.path)
            result = {"operation": "workspace_created", "isolated_copy_only": True}
        else:
            workspace = open_config_workspace(args.workspace)
            input_snapshots = []
            preview: AccountConfigPreview | AutoexecPreview | GsiConfigPreview
            if args.command == "account":
                settings_text, settings_snapshot = _input(args.settings, workspace)
                settings = SettingsStore.loads(settings_text)
                input_snapshots.append((args.settings, "utf-8-sig", settings_snapshot))
                mapped = None
                if args.localconfig_copy:
                    mapped, local_snapshot = _input(args.localconfig_copy, workspace, args.encoding)
                    input_snapshots.append((args.localconfig_copy, args.encoding, local_snapshot))
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
                    auxiliary_sources=tuple(snapshot for _, _, snapshot in input_snapshots),
                )
                changed = True  # Legacy writer rewrites even equal account configs.
            elif args.command == "autoexec":
                preview = preview_autoexec(
                    workspace,
                    source_path=args.source,
                    config_file_name=args.config_file_name,
                    encoding=args.encoding,
                )
                changed = preview.plan.changed
            else:
                preview = preview_gsi_config(
                    workspace,
                    running_on_port=args.port,
                    gsi_file_index=args.index,
                    token_source=args.token_source,
                    encoding=args.encoding,
                )
                changed = True
            fingerprint = preview_fingerprint((preview, input_snapshots))
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
                for path, encoding, snapshot in input_snapshots:
                    if _input(path, workspace, encoding)[1] != snapshot:
                        raise ConfigStalePreviewError()
                if isinstance(preview, AccountConfigPreview):
                    applied = apply_account_config(preview)
                elif isinstance(preview, AutoexecPreview):
                    applied = apply_autoexec(preview)
                else:
                    applied = apply_gsi_config(preview)
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
