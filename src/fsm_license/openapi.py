"""Export the public contract without production secrets or a database connection."""

import argparse
import json
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .app import create_app
from .config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    app = create_app(
        Settings(
            database_url="sqlite+pysqlite:///:memory:",
            internal_token=secrets.token_urlsafe(48),
            signing_key=Ed25519PrivateKey.generate().private_bytes_raw(),
        )
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(app.openapi(), indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
