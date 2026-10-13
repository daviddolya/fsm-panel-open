"""Small Ed25519 JWS/proof wire format, shared by the service and desktop."""

import base64
import hashlib
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def b64decode(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def canonical_json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def key_fingerprint(public_key: bytes) -> str:
    return hashlib.sha256(public_key).hexdigest()


def activation_key_hash(value: str) -> str:
    # Keys contain 256 random bits, so a fast one-way hash is appropriate.
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def proof_message(method: str, target: str, timestamp: str, nonce: str, body: bytes) -> bytes:
    return (
        f"FSM-PROOF-1\n{method.upper()}\n{target}\n{timestamp}\n{nonce}\n"
        f"{hashlib.sha256(body).hexdigest()}"
    ).encode()


def sign_lease(claims: dict, private_key: bytes, key_id: str) -> str:
    header = b64encode(canonical_json({"alg": "EdDSA", "typ": "JWT", "kid": key_id}))
    payload = b64encode(canonical_json(claims))
    message = f"{header}.{payload}".encode("ascii")
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(message)
    return f"{header}.{payload}.{b64encode(signature)}"
