"""Export an explicit offline launch proposal to a new private JSON file."""

import argparse
import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from fsm_panel.adapters.launch_plan import (
    LaunchFacts,
    LaunchPlanError,
    LaunchSettings,
    build_launch_plan,
)

MAX_INPUT_BYTES = 2 * 1024 * 1024


def preview(input_path: Path, output_path: Path) -> dict[str, object]:
    with input_path.open("rb") as source:
        raw = source.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise LaunchPlanError("launch_input_too_large")
    try:
        document = json.loads(raw)
        plan = build_launch_plan(
            LaunchSettings(**document["settings"]),
            login=document["login"],
            update_mode=document["update_mode"],
            environment=document["environment"],
            facts=LaunchFacts(**document["facts"]),
            ipc_random=document.get("ipc_random"),
            steam_path=document.get("steam_path"),
        )
    except KeyError, TypeError, UnicodeError, json.JSONDecodeError, RecursionError:
        raise LaunchPlanError("launch_invalid_input") from None
    # Never infer environment or filesystem facts from the current host. Inherited
    # environment may contain secrets; only legacy overrides are exported.
    output = {
        "kind": "offline_launch_proposal",
        "executable": False,
        "argv": plan.argv,
        "status": plan.status,
        "used_port": plan.used_port,
        "creationflags": plan.creationflags,
        "shell": plan.shell,
        "environment_mode": "inherit" if plan.environment is None else "copy_with_overrides",
        "environment_overrides": None
        if plan.environment is None
        else {key: plan.environment[key] for key in ("VPROJECT", "USERPROFILE", "LOCALAPPDATA")},
        "profile_directory": asdict(plan.profile_directory) if plan.profile_directory else None,
        "shader_cache": None
        if plan.shader_cache is None
        else {
            "operations": [
                {"kind": type(action).__name__, **asdict(action)}
                for action in plan.shader_cache.operations
            ],
            "caught_error": plan.shader_cache.caught_error,
            "on_exception": plan.shader_cache.on_exception,
        },
        "unimplemented": [
            "config_and_registry_effects",
            "entry_gating_and_locking",
            "process_launch_and_window_detection",
            "runtime_parity",
        ],
    }
    encoded = json.dumps(output, ensure_ascii=False, indent=2) + "\n"
    encoded.encode("utf-8")  # Validate before creating even an empty output file.
    # Exclusive creation also protects an existing input/evidence file. Command
    # arguments can contain caller-provided secrets: output belongs in private data.
    with output_path.open("x", encoding="utf-8") as destination:
        destination.write(encoded)
    return {
        "operation": "preview_only",
        "argument_count": len(plan.argv),
        "environment_mode": output["environment_mode"],
        "output_written": True,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New private JSON file")
    args = parser.parse_args(argv)
    try:
        result = preview(args.input, args.output)
    except LaunchPlanError as error:
        result = {"error": error.code}
    except FileExistsError:
        result = {"error": "launch_output_exists"}
    except OSError:
        result = {"error": "launch_file_unavailable"}
    except UnicodeError:
        result = {"error": "launch_invalid_unicode"}
    print(json.dumps(result))
    return 2 if "error" in result else 0


if __name__ == "__main__":
    raise SystemExit(main())
