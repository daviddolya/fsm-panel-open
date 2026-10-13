"""Read-only source/provenance audit. Never imports or executes the supplied looter.

Compares installed direct SDK package files with integrity-verified public npm tarballs.
Only named code/package files are read; account/session/maFile paths are never traversed.
"""

import argparse
import base64
import hashlib
import io
import json
import tarfile
import urllib.request
from pathlib import Path

PACKAGES = (
    "steam-user",
    "steamcommunity",
    "steam-tradeoffer-manager",
    "steam-session",
    "steam-totp",
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as response:
        content = response.read(30_000_001)
    if len(content) > 30_000_000:
        raise ValueError("public package too large")
    return content


def audit(root: Path) -> dict:
    root = root.resolve(strict=True)
    source = (root / "looter_core.js").read_bytes()
    lock_bytes = (root / "package-lock.json").read_bytes()
    lock = json.loads(lock_bytes)
    packages = []
    for name in PACKAGES:
        installed_root = root / "node_modules" / name
        installed = json.loads((installed_root / "package.json").read_bytes())
        version = installed["version"]
        metadata_url = f"https://registry.npmjs.org/{name}/{version}"
        metadata = json.loads(fetch(metadata_url))
        archive = fetch(metadata["dist"]["tarball"])
        algorithm, expected = metadata["dist"]["integrity"].split("-", 1)
        if (
            algorithm != "sha512"
            or base64.b64encode(hashlib.sha512(archive).digest()).decode() != expected
        ):
            raise ValueError("package integrity mismatch")
        compared, differences, missing = 0, [], []
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as package:
            for member in package.getmembers():
                if not member.isfile() or not member.name.startswith("package/"):
                    continue
                relative = member.name.removeprefix("package/")
                target = (installed_root / relative).resolve()
                if not target.is_relative_to(installed_root.resolve()):
                    raise ValueError("invalid package path")
                if not target.is_file():
                    missing.append(relative)
                    continue
                handle = package.extractfile(member)
                assert handle is not None
                clean = handle.read()
                actual = target.read_bytes()
                compared += 1
                if clean != actual:
                    differences.append(
                        {
                            "file": relative,
                            "installed_sha256": digest(actual),
                            "upstream_sha256": digest(clean),
                        }
                    )
        locked = lock.get("packages", {}).get("node_modules/" + name, {}) or lock.get(
            "dependencies", {}
        ).get(name, {})
        packages.append(
            {
                "name": name,
                "installed_version": version,
                "locked_version": locked.get("version"),
                "license": metadata.get("license"),
                "metadata_url": metadata_url,
                "tarball": metadata["dist"]["tarball"],
                "integrity": metadata["dist"]["integrity"],
                "integrity_verified": True,
                "compared_files": compared,
                "different_files": differences,
                "missing_files": missing,
            }
        )
    return {
        "format_version": 1,
        "observed_on": "2026-10-13",
        "scope": "Static code audit only; original and SDK code were not executed.",
        "legacy_source": "FSM_PANEL v.3.1.2/looter/looter_core.js",
        "source_sha256": digest(source),
        "lock_sha256": digest(lock_bytes),
        "packages": packages,
        "observations": [
            {
                "lines": "7-27",
                "behavior": "Credentials and trade URL in argv; default inventory 730/2.",
            },
            {"lines": "30-52", "behavior": "SteamUser logOn with TOTP, login errors exit -1."},
            {
                "lines": "54-96",
                "behavior": "Every webSession starts inventory callbacks; per-inventory failures skipped.",
            },
            {
                "lines": "98-112",
                "behavior": "One offer after all inventory callbacks; pending confirmation exits 1.",
            },
            {
                "lines": "100-117",
                "behavior": "Sent status has no explicit shutdown; no repeated-session guard.",
            },
            {"lines": "120-125", "behavior": "Raw error converted to stdout text before exit -1."},
        ],
        "limitations": [
            "Static findings do not establish live Steam behavior or legacy panel invocation.",
            "Only files included in each npm archive are compared; extra installed files are not inventoried.",
            "Registry integrity verifies download bytes against registry metadata, not publisher identity.",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("legacy_root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    output = audit(args.legacy_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2) + "\n", encoding="utf8")
