"""Rebuild the evidence ledger from inventory and reviewed static metadata.

Implementation status describes local contracts, never runtime parity. Editing this
generator keeps the checked-in ledger reproducible without shipping private settings.
"""

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("inventory", type=Path)
    args = parser.parse_args()
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    root = Path(__file__).resolve().parents[1]
    catalogue = json.loads((root / "research/feature-catalogue.json").read_text(encoding="utf-8"))
    features = []
    for key, value_type in inventory["settings_types"].items():
        features.append(
            {
                "feature_id": f"SETTING.{key}",
                "action": f"Import/export {key}",
                "input_type": value_type,
                "evidence": "settings/settings.json (key/type only)",
                "confidence": "observed-schema",
                "original_consumer": "partial_static_references_only",
                "native_references": [
                    {"address": item["native_unwind_root"], "source_line": item["source_line"]}
                    for item in catalogue["functions"]
                    if key in item["config_key_references"]
                ],
                "expected_result": "Exact observed type; preserve unknown keys; redact exported secrets",
                "preconditions": "User explicitly imports a legacy JSON copy",
                "state_changes": "Versioned DPAPI-backed settings vault",
                "external_effects": "Encrypted file and backup; no game configuration writes",
                "implementation": "schema_import_export_implemented; game_consumer_not_implemented",
                "runtime_parity": "not_verified",
                "tests": ["tests/core/test_settings.py", "tests/adapters/test_settings_vault.py"],
            }
        )
    for identifier, action, evidence in [
        ("TRADE.LOGIN", "Steam login and TOTP", "looter_core.js:34-50"),
        (
            "TRADE.INVENTORY",
            "Parallel tradable inventories; tolerate partial failures",
            "looter_core.js:59-95",
        ),
        ("TRADE.SEND", "One outgoing offer from successful inventories", "looter_core.js:97-108"),
        ("TRADE.CONFIRM", "Confirm pending offer", "looter_core.js:100-108"),
        ("CPU.BES", "CPU limiter process", "BES/src/hack.cpp"),
    ]:
        features.append(
            {
                "feature_id": identifier,
                "action": action,
                "evidence": evidence,
                "confidence": "observed-source",
                "implementation": "owned_adapter_and_gui_implemented_stand_pending"
                if identifier == "CPU.BES"
                else "worker_implemented_stand_pending",
                "runtime_parity": "not_verified",
                "tests": [
                    "tests/adapters/test_bes.py",
                    "tests/test_cpu.py",
                    "tests/ui/test_cpu_panel.py",
                ]
                if identifier == "CPU.BES"
                else ["worker/tests", "tests/trading", "tests/qa"],
            }
        )
    for identifier in [
        "GUI.ALL",
        "GAME.LAUNCH",
        "GAME.SANDBOX",
        "GAME.LOBBY",
        "GAME.SEARCH",
        "GAME.AUTOACCEPT",
        "GAME.ANTIAFK",
        "GAME.RECONNECT",
        "GAME.BATCHES",
        "DROP.STATISTICS",
        "LEGACY.LICENSE",
    ]:
        features.append(
            {
                "feature_id": identifier,
                "evidence": "research/STATIC_FINDINGS.md; research/feature-catalogue.json (partial static candidates)",
                "confidence": "partial_static; algorithms_and_error_paths_incomplete",
                "implementation": "blocked_specification",
                "runtime_parity": "blocked_original_license",
                "tests": [],
            }
        )
    for identifier, action, status, evidence, tests in [
        (
            "GAME.LAUNCH.PREPARE",
            "Pure argv, profile/shader, conditional environment, loginusers and registry proposals",
            "implemented_offline_planner_no_executor",
            "Panel.exe 0x14067ae90; docs/launch-plan.md",
            ["tests/adapters/test_launch_plan.py", "tests/test_launch_preview.py"],
        ),
        (
            "ACCOUNT.USERDATA_ID",
            "Exact integer SteamID minus 76561197960265728",
            "implemented_pure_helper",
            "Panel.exe 0x140bfb190 and 0x142d0d7b0; docs/launch-plan.md",
            ["tests/adapters/test_launch_plan.py"],
        ),
        (
            "VIDEO.PREPARE",
            "Preview and update a separate panel video template from Steam GPU IDs",
            "implemented_fields_default_and_explicit_legacy_mode",
            "Panel.exe 0x1406787d0; docs/video.md",
            [
                "tests/adapters/test_video.py",
                "tests/ui/test_video_panel.py",
                "tests/test_maintenance.py",
                "tests/qa/test_offline_video.py",
            ],
        ),
        (
            "NEW.CORE",
            "Account lifecycle, cancellation, recovery and 50-session limit",
            "implemented_simulator",
            "Approved new architecture; no original lifecycle claim",
            ["tests/core", "scripts/verify_simulator.py"],
        ),
        (
            "NEW.GUI",
            "Accounts, settings, video preview/apply, owned CPU controls, simulation trades, license and journal",
            "implemented_partial_ui",
            "Approved new UI; legacy screen catalogue incomplete",
            ["tests/ui", "tests/qa"],
        ),
        (
            "NEW.LICENSE",
            "Online activation, 60-second leases, external entitlements and transactional device limits",
            "implemented_local_postgres_verified",
            "Approved new licensing contract",
            [
                "tests/licensing",
                "tests/qa/test_license_failure_boundaries.py",
                "scripts/verify_license_container.py",
            ],
        ),
        (
            "NEW.SECRETS",
            "DPAPI secrets, device identity and encrypted settings",
            "implemented_windows_verified",
            "Approved new secret-storage contract",
            ["tests/adapters/test_secrets.py", "tests/adapters/test_settings_vault.py"],
        ),
        (
            "NEW.PROCESSES",
            "Own processes by PID, creation time and executable; clean up owned children",
            "implemented_disposable_child_verified",
            "Approved ownership contract; no CS2 behavior claim",
            ["tests/adapters/test_processes.py", "tests/adapters/test_processes_live_local.py"],
        ),
        (
            "GSI.RECEIVE",
            "Store last game-state payload for a registered account",
            "implemented_defensive_contract",
            "Manual Ghidra: 0x1409ffad0 and 0x1409ff130; docs/gsi.md",
            ["tests/gsi"],
        ),
        (
            "TRADE.SELECTED",
            "Selected assets, comments, refresh token, proxy and grouped offers",
            "worker_implemented_stand_pending",
            "Embedded script SHA256 d010c676ce33adaee529a79fafc9501fd2ca39c3ca0794e90680ff7cb681302c",
            ["worker/tests", "tests/trading"],
        ),
        (
            "STEAM.ACTIVITY",
            "Free app entitlement request and bounded games-played session",
            "worker_implemented_stand_pending",
            "Embedded script SHA256 3349e3c1232551beb4f90f1849c39f289c84e014df0e0cbce07e948f70964f8c",
            ["worker/tests", "tests/trading"],
        ),
        (
            "NOTIFY.TELEGRAM",
            "Original text/image notification scenarios",
            "blocked_specification",
            "Dependency and settings only; complete scenario contract unresolved",
            [],
        ),
    ]:
        features.append(
            {
                "feature_id": identifier,
                "action": action,
                "evidence": evidence,
                "confidence": "new_contract"
                if identifier.startswith("NEW.")
                else "static_contract",
                "implementation": status,
                "runtime_parity": "not_applicable"
                if identifier.startswith("NEW.")
                else "not_verified",
                "tests": tests,
            }
        )
    for item in features:
        item.setdefault(
            "preconditions",
            "See linked subsystem contract; original unresolved where not documented",
        )
        item.setdefault("input", item.get("input_type", "See linked contract"))
        item.setdefault(
            "expected_result",
            "See linked contract; unresolved original behavior is not an acceptance oracle",
        )
        item.setdefault("state_changes", "See linked contract")
        item.setdefault(
            "external_effects", "See linked contract; live effects deferred to the stand"
        )
        item["verification"] = (
            "stand_not_run"
            if not item["feature_id"].startswith("NEW.")
            else "see docs/VERIFICATION.md"
        )
    output = Path(__file__).resolve().parents[1] / "docs" / "feature-matrix.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "catalog_complete": False,
                "original_sha256": "3731a48590e3169071b81aab9338a36b38a788133adfd43fada99ad3add1f308",
                "features": features,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
