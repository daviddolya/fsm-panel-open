"""Reproducible, offline metadata inventory. Never records account or setting values."""

import argparse
import hashlib
import json
from pathlib import Path


def inventory(root: Path) -> dict:
    records = []
    count = size = 0
    for file in sorted(root.rglob("*")):
        if not file.is_file():
            continue
        count += 1
        size += file.stat().st_size
        relative = file.relative_to(root).as_posix()
        if relative.startswith(("maFiles/", "sessions/")) or relative == "logpass.txt":
            continue
        records.append(
            {
                "path": relative,
                "bytes": file.stat().st_size,
                "sha256": hashlib.file_digest(file.open("rb"), "sha256").hexdigest(),
            }
        )
    settings = json.loads((root / "settings/settings.json").read_text(encoding="utf-8-sig"))
    lock = json.loads((root / "looter/package-lock.json").read_text(encoding="utf-8"))
    packages = []
    for name in (
        "steam-user",
        "steamcommunity",
        "steam-totp",
        "steam-tradeoffer-manager",
        "steam-session",
    ):
        actual = json.loads(
            (root / f"looter/node_modules/{name}/package.json").read_text(encoding="utf-8")
        )
        locked = lock["packages"][f"node_modules/{name}"]["version"]
        packages.append(
            {
                "name": name,
                "installed": actual["version"],
                "locked": locked,
                "matches_lock": locked == actual["version"],
            }
        )
    return {
        "evidence_type": "offline-file-inventory",
        "runtime_observed": False,
        "files": count,
        "bytes": size,
        "settings_types": {k: type(v).__name__ for k, v in settings.items()},
        "packages": packages,
        "artifacts": records,
        "limitations": [
            "Secret-bearing account files omitted",
            "Setting values omitted",
            "Presence does not prove execution",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("original", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = inventory(args.original.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "files": result["files"],
                "bytes": result["bytes"],
                "settings": len(result["settings_types"]),
            }
        )
    )


if __name__ == "__main__":
    main()
