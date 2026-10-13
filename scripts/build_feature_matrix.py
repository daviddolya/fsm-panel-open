"""Build the initial evidence ledger; never infer game behavior from setting names."""

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("inventory", type=Path)
    args = parser.parse_args()
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    features = []
    for key, value_type in inventory["settings_types"].items():
        features.append(
            {
                "feature_id": f"SETTING.{key}",
                "action": f"Import/export {key}",
                "input_type": value_type,
                "evidence": "settings/settings.json (key/type only)",
                "confidence": "observed-schema",
                "original_consumer": "unknown",
                "implementation": "pending",
                "runtime_parity": "not_verified",
                "tests": [],
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
                "implementation": "pending",
                "runtime_parity": "not_verified",
                "tests": [],
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
                "evidence": "Not yet recovered from Panel.exe",
                "confidence": "unknown",
                "implementation": "blocked_specification",
                "runtime_parity": "blocked_original_license",
                "tests": [],
            }
        )
    output = Path(__file__).resolve().parents[1] / "docs" / "feature-matrix.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "schema_version": 1,
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
