"""Generate fresh deployment credentials. Output is secret; do not commit or log it."""

import secrets

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .crypto import b64encode


def main() -> None:
    key = Ed25519PrivateKey.generate()
    print(f"FSM_LICENSE_SIGNING_KEY={b64encode(key.private_bytes_raw())}")
    print(f"FSM_LICENSE_PUBLIC_KEY={b64encode(key.public_key().public_bytes_raw())}")
    print(f"FSM_LICENSE_INTERNAL_TOKEN={secrets.token_urlsafe(48)}")
    print(f"POSTGRES_PASSWORD={secrets.token_urlsafe(32)}")


if __name__ == "__main__":
    main()
