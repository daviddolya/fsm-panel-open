"""Build a labelled research desktop; never imply missing integration is release-ready."""

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from collect_notices import collect


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--research-build", action="store_true", required=True)
    parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    options = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--name",
        "FSMPanelOpen",
        "--paths",
        str(root / "src"),
        "--distpath",
        str(root / "dist"),
        "--workpath",
        str(root / "build"),
        "--specpath",
        str(root / "build"),
        "--exclude-module",
        "PySide6.QtWebEngineCore",
        "--exclude-module",
        "PySide6.QtWebEngineWidgets",
    ]
    subprocess.run(options + [str(root / "src/fsm_panel/__main__.py")], cwd=root, check=True)
    target = root / "dist/FSMPanelOpen"
    for name in ("LICENSE", "README.md"):
        shutil.copy2(root / name, target / name)
    shutil.copy2(root / "docs/STATUS.md", target / "RESEARCH-STATUS.md")
    shutil.copytree(root / "docs", target / "docs", dirs_exist_ok=True)
    shutil.copytree(root / "contracts", target / "contracts", dirs_exist_ok=True)
    shutil.copy2(root / "FSM_PANEL_REBUILD_PLAN.md", target / "FSM_PANEL_REBUILD_PLAN.md")
    subprocess.run(
        [sys.executable, str(root / "worker/scripts/stage_worker.py"), str(target / "worker")],
        cwd=root,
        check=True,
    )
    collect(root, target / "third-party-notices")
    shutil.copy2(root / "licenses/Node-24.19.0-LICENSE.txt", target / "worker/NODE-LICENSE.txt")
    manifest = {
        "kind": "research_build",
        "parity": "not_verified",
        "python": platform.python_version(),
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "source_dirty": bool(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip()
        ),
        "files": {
            path.relative_to(target).as_posix(): digest(path)
            for path in sorted(target.rglob("*"))
            if path.is_file()
        },
    }
    (target / "build-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(target)


if __name__ == "__main__":
    main()
