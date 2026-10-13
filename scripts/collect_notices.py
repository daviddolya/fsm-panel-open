"""Inventory locked runtime packages and copy installed license metadata for a build."""

import argparse
import importlib.metadata
import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def collect(root: Path, destination: Path) -> None:
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    packages = {normalized(item["name"]): item for item in lock["package"]}
    pending = ["fsm-panel-open"]
    selected: set[str] = set()
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        selected.add(name)
        pending.extend(normalized(item["name"]) for item in packages[name].get("dependencies", []))
    rows = []
    destination.mkdir(parents=True, exist_ok=True)
    for name in sorted(selected - {"fsm-panel-open"}):
        record = {"name": name, "locked_version": packages[name]["version"]}
        try:
            distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            record["installed_on_this_platform"] = False
        else:
            record.update(
                {
                    "installed_on_this_platform": True,
                    "version": distribution.version,
                    "declared_license": distribution.metadata.get("License-Expression")
                    or distribution.metadata.get("License", "not declared"),
                }
            )
            package_dir = destination / "python" / name
            package_dir.mkdir(parents=True, exist_ok=True)
            (package_dir / "METADATA.txt").write_text(
                distribution.read_text("METADATA") or "Metadata unavailable\n", encoding="utf-8"
            )
            copied = []
            for entry in distribution.files or []:
                path = Path(str(entry))
                if (
                    "licenses" in path.parts
                    or path.name.upper().startswith(("LICENSE", "COPYING", "NOTICE"))
                ) and path.suffix not in {".py", ".pyc"}:
                    source = Path(distribution.locate_file(entry))
                    if source.is_file():
                        relative = Path(*[part for part in path.parts if part not in {"..", "."}])
                        output = package_dir / relative
                        output.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, output)
                        copied.append(relative.as_posix())
            record["license_files"] = copied
        rows.append(record)
    (destination / "python-components.json").write_text(
        json.dumps(
            {
                "scope": "locked runtime graph; freezer may omit unused modules; not legal clearance",
                "components": rows,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    node = shutil.which("node")
    if node:
        with (destination / "NODE-LICENSE.txt").open("w", encoding="utf-8") as output:
            subprocess.run([node, "--license"], stdout=output, check=True, text=True)
    print(f"Recorded {len(rows)} Python runtime dependency entries")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    collect(Path(__file__).resolve().parents[1], args.output)


if __name__ == "__main__":
    main()
