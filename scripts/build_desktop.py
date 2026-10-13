"""Build a labelled research desktop; never imply missing integration is release-ready."""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


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
    worker = root / "worker/dist"
    if worker.is_dir():
        shutil.copytree(worker, target / "worker", dirs_exist_ok=True)
        node = shutil.which("node")
        if node:
            shutil.copy2(node, target / "node.exe")
    print(target)


if __name__ == "__main__":
    main()
