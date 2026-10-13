"""Run a packaged synthetic desktop without developer Python/Node paths.

This checks relocation/runtime packaging on this host, not a clean-machine install.
"""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

import psutil


def verify(executable: Path, output: Path) -> None:
    executable = executable.resolve(strict=True)
    environment = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "NODE_PATH", "NODE_OPTIONS"):
        environment.pop(name, None)
    windows = Path(environment.get("WINDIR", r"C:\Windows"))
    environment["PATH"] = os.pathsep.join([str(windows / "System32"), str(windows)])
    environment["QT_QPA_PLATFORM"] = "offscreen"
    with tempfile.TemporaryDirectory(prefix="fsm packaged проверка ") as directory:
        temporary = Path(directory)
        report = temporary / "result.json"
        process = subprocess.Popen(
            [
                str(executable),
                "--simulate",
                "--smoke-test",
                "--data-dir",
                str(temporary / "data"),
                "--smoke-report",
                str(report),
            ],
            cwd=temporary,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        owned = psutil.Process(process.pid)
        try:
            process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            for child in owned.children(recursive=True):
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            process.kill()
            process.communicate()
            raise RuntimeError("packaged_smoke_timeout") from None
        if not report.is_file():
            raise RuntimeError("packaged_smoke_failed")
        result = json.loads(report.read_text(encoding="utf-8"))
        result.update(
            {
                "developer_runtime_paths": "removed",
                "cwd": "external_unicode_temp",
                "clean_machine": "not_verified",
                "exit_code": process.returncode,
            }
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    if process.returncode or result.get("passed") is not True:
        raise RuntimeError("packaged_smoke_failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    verify(args.executable, args.output)
