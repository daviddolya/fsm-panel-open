"""Build a relocatable Windows worker directory with production dependencies and Node.

The destination must not exist. No source or unrelated directory is removed.
Hoisted production installation avoids absolute Windows pnpm junctions in artifacts.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def run(command: list[str], cwd: Path, *, data: str | None = None) -> str:
    result = subprocess.run(command, cwd=cwd, input=data, text=True, capture_output=True,
                            timeout=300, check=False,
                            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    if result.returncode:
        raise RuntimeError("worker_staging_command_failed")
    return result.stdout


def stage(output: Path, *, skip_build: bool = False, node_path: str | None = None) -> dict:
    source = Path(__file__).resolve().parents[1]
    output = output.resolve()
    node, pnpm = node_path or shutil.which("node"), shutil.which("pnpm")
    if not node or not pnpm or output.exists() or output.is_relative_to(source / "node_modules"):
        raise ValueError("invalid_staging_environment_or_destination")
    version = run([node, "--version"], source).strip()
    if not version.startswith("v24."):
        raise ValueError("node24_required")
    if not skip_build:
        run([pnpm, "--dir", str(source), "install", "--frozen-lockfile"], source)
        run([pnpm, "--dir", str(source), "build"], source)
    if not (source / "dist/main.js").is_file():
        raise ValueError("worker_build_required")
    output.mkdir(parents=True)
    for name in ("package.json", "pnpm-lock.yaml"):
        shutil.copy2(source / name, output / name)
    (output / "pnpm-workspace.yaml").write_text(
        (source / "pnpm-workspace.yaml").read_text() + "\nnodeLinker: hoisted\n", encoding="utf8")
    shutil.copytree(source / "dist", output / "dist")
    shutil.copytree(source / "audit", output / "audit")
    run([pnpm, "--dir", str(output), "install", "--prod", "--frozen-lockfile"], source)
    for entry in output.rglob("*"):
        if entry.is_symlink() or entry.is_junction():
            raise RuntimeError("nonportable_dependency_link")
    runtime = output / ("node.exe" if sys.platform == "win32" else "node")
    shutil.copy2(node, runtime)
    run([str(runtime), "--input-type=module", "-e",
         "import {loadSdk} from './dist/live.js'; loadSdk();"], output)
    with tempfile.TemporaryDirectory(prefix="fsm-worker-staging-") as temporary:
        request = {"version": 1, "request_id": "staging-smoke", "method": "status",
                   "timeout_ms": 1000, "params": {"account_id": "synthetic", "operation_id": "missing"}}
        response = run([str(runtime), "dist/main.js", "--state", str(Path(temporary) / "worker.sqlite")],
                       output, data=json.dumps(request) + "\n")
        if json.loads(response).get("error", {}).get("code") != "operation_not_found":
            raise RuntimeError("staged_worker_smoke_failed")
    manifest = {"node_version": version, "node_sha256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
                "worker_version": json.loads((source / "package.json").read_text())["version"],
                "dependency_layout": "hoisted_no_links", "sdk_import_smoke": "passed",
                "simulation_ipc_smoke": "passed", "live_stand": "not_run",
                "dependency_audit": "audit/dependency-audit.json"}
    (output / "audit/runtime-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--node")
    args = parser.parse_args()
    print(json.dumps(stage(args.output, skip_build=args.skip_build, node_path=args.node), indent=2))
