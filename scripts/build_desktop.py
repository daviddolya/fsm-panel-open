"""Build a labelled research desktop; never imply missing integration is release-ready."""

import argparse
import hashlib
import json
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pefile
from collect_notices import collect


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def use_windows_icu(target: Path) -> list[dict[str, str]]:
    """Qt uses Windows ICU's unversioned exports, not an unrelated PATH ICU.

    The freezer can select Git's icuuc.dll with only *_78 exports. Verify the
    actual Qt imports against the host OS and remove that generated shadow copy.
    The Windows system library itself is neither modified nor redistributed.
    """
    import os

    internal = target / "_internal"
    with pefile.PE(str(internal / "PySide6/Qt6Core.dll")) as qt:
        required = {
            symbol.name
            for library in qt.DIRECTORY_ENTRY_IMPORT
            if library.dll.lower() == b"icuuc.dll"
            for symbol in library.imports
            if symbol.name
        }
    if not required:
        return []
    system = Path(os.environ["WINDIR"]) / "System32/icuuc.dll"
    with pefile.PE(str(system)) as os_icu:
        exported = {symbol.name for symbol in os_icu.DIRECTORY_ENTRY_EXPORT.symbols}
    if not required <= exported:
        raise RuntimeError("The target Qt runtime requires compatible Windows system ICU")
    bundled = internal / "icuuc.dll"
    if not bundled.exists():
        return []
    with pefile.PE(str(bundled)) as foreign:
        supporting = [
            library.dll.decode("ascii")
            for library in foreign.DIRECTORY_ENTRY_IMPORT
            if re.fullmatch(rb"icudt\d+\.dll", library.dll, re.IGNORECASE)
        ]
    excluded = []
    for candidate in [bundled, *(internal / name for name in supporting)]:
        if candidate.exists():
            if not candidate.resolve().is_relative_to(target.resolve()):
                raise RuntimeError("Unexpected binary staging path")
            excluded.append(
                {
                    "name": candidate.name,
                    "sha256": digest(candidate),
                    "reason": "use_verified_Windows_system_ICU",
                }
            )
            candidate.unlink()
    return excluded


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--research-build", action="store_true", required=True)
    parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip():
        raise RuntimeError("Commit the reviewed source before building its matching source archive")
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
        "--collect-data",
        "fsm_panel.config_templates",
    ]
    subprocess.run(options + [str(root / "src/fsm_panel/__main__.py")], cwd=root, check=True)
    target = root / "dist/FSMPanelOpen"
    excluded_binaries = use_windows_icu(target)
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
    node_components = []
    for package in sorted((target / "worker/node_modules").rglob("package.json")):
        item = json.loads(package.read_text(encoding="utf-8"))
        if item.get("name") and item.get("version"):
            node_components.append(
                {
                    "name": item["name"],
                    "version": item["version"],
                    "declared_license": item.get("license", item.get("licenses", "not declared")),
                    "path": package.relative_to(target).as_posix(),
                }
            )
    (target / "third-party-notices/node-components.json").write_text(
        json.dumps(
            {
                "scope": "package manifests present in portable runtime; nested fixtures may appear",
                "components": node_components,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            "git",
            "archive",
            "--format=zip",
            "--prefix=fsm-panel-open/",
            "--output=" + str(root / "dist/FSMPanelOpen-source.zip"),
            "HEAD",
        ],
        cwd=root,
        check=True,
    )
    (target / "SOURCE.md").write_text(
        "# Corresponding source\n\nThe reviewed source accompanies this research build in "
        "`../FSMPanelOpen-source.zip`. It contains build instructions and all new GPL-3.0-only code. "
        "Third-party package notices are in `third-party-notices`; Node dependencies include their "
        "installed source and license files. No original proprietary executable is redistributed.\n",
        encoding="utf-8",
    )
    manifest = {
        "kind": "research_build",
        "parity": "not_verified",
        "python": platform.python_version(),
        "excluded_binaries": excluded_binaries,
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
