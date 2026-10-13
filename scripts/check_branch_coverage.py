"""Enforce actual branch coverage, separately from coverage.py's combined percentage."""

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--minimum", type=float, default=90)
    args = parser.parse_args()
    data = json.loads(args.report.read_text(encoding="utf-8"))
    total = covered = 0
    for name, record in data["files"].items():
        if "fsm_panel/core/" in name.replace("\\", "/"):
            total += record["summary"]["num_branches"]
            covered += record["summary"]["covered_branches"]
    if total == 0:
        raise SystemExit("No core branch data: use --cov=fsm_panel.core --cov-branch")
    percent = 100 * covered / total
    print(f"Core branches: {covered}/{total} = {percent:.2f}% (minimum {args.minimum:.2f}%)")
    if percent < args.minimum:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
