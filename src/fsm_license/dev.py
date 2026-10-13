"""Local loopback launcher with fresh process-only secrets and explicit database choice."""

import argparse
import secrets
from pathlib import Path

import uvicorn
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from alembic import command

from .app import create_app
from .config import Settings
from .crypto import b64encode


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", help="dedicated development PostgreSQL database")
    parser.add_argument("--sqlite", type=Path, help="single-process functional testing only")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if bool(args.database_url) == bool(args.sqlite):
        parser.error("select exactly one of --database-url or --sqlite")
    if args.sqlite is not None:
        args.sqlite.parent.mkdir(parents=True, exist_ok=True)
    url = args.database_url or "sqlite+pysqlite:///" + args.sqlite.resolve().as_posix()
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.attributes["database_url"] = url
    command.upgrade(config, "head")
    signing = Ed25519PrivateKey.generate()
    settings = Settings(
        database_url=url,
        internal_token=secrets.token_urlsafe(48),
        signing_key=signing.private_bytes_raw(),
        issuer="fsm-license-dev",
        audience="fsm-panel-dev",
        key_id="dev-process",
    )
    print("Temporary local development credentials (do not commit):", flush=True)
    print(f"FSM_LICENSE_URL=http://127.0.0.1:{args.port}", flush=True)
    print(f"FSM_LICENSE_INTERNAL_TOKEN={settings.internal_token}", flush=True)
    print(
        f"FSM_LICENSE_PUBLIC_KEY={b64encode(signing.public_key().public_bytes_raw())}", flush=True
    )
    print("FSM_LICENSE_ISSUER=fsm-license-dev\nFSM_LICENSE_AUDIENCE=fsm-panel-dev", flush=True)
    print("FSM_LICENSE_KEY_ID=dev-process", flush=True)
    uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
