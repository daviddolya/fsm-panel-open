"""Offline operator commands. Explicit files only; no original or game execution."""

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from fsm_panel.adapters.video import (
    VideoApplyError,
    VideoTemplateError,
    apply_video_template,
    preview_video_template,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FSM Panel Open: local maintenance")
    commands = parser.add_subparsers(dest="command", required=True)
    video = commands.add_parser("video", help="Preview or update a copied panel video template")
    video.add_argument("--template", type=Path, required=True)
    video.add_argument("--steam-config", type=Path, required=True)
    video.add_argument("--policy", choices=("fields", "legacy"), default="fields")
    video.add_argument(
        "--apply", action="store_true", help="Back up and atomically update template"
    )
    video.add_argument("--expect-template-sha256")
    video.add_argument("--expect-source-sha256")
    args = parser.parse_args(argv)
    try:
        preview = preview_video_template(args.template, args.steam_config, policy=args.policy)
        if (
            args.expect_template_sha256 is not None
            and args.expect_template_sha256 != preview.template_sha256
        ) or (
            args.expect_source_sha256 is not None
            and args.expect_source_sha256 != preview.steam_config_sha256
        ):
            print(json.dumps({"error": "video_preview_stale"}))
            return 2
        result: dict[str, object] = {
            "operation": "apply" if args.apply else "preview",
            "policy": preview.policy,
            "changed": preview.changed,
            "template_sha256": preview.template_sha256,
            "source_sha256": preview.steam_config_sha256,
            "before": {"VendorID": preview.before_vendor_id, "DeviceID": preview.before_device_id},
            "after": {"VendorID": preview.after_vendor_id, "DeviceID": preview.after_device_id},
        }
        if args.apply:
            applied = apply_video_template(preview)
            result["backup_path"] = str(applied.backup_path) if applied.backup_path else None
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except VideoApplyError as error:
        print(
            json.dumps(
                {
                    "error": error.code,
                    "backup_path": str(error.backup_path) if error.backup_path else None,
                    "rollback_succeeded": error.rollback_succeeded,
                }
            )
        )
        return 2
    except VideoTemplateError as error:
        print(json.dumps({"error": error.code}))
        return 2
    except OSError:
        print(json.dumps({"error": "video_file_unavailable"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
