"""Service-authenticated provisioning and lost-device transfer CLI."""

import argparse
import ipaddress
import json
import os
from urllib.parse import quote, urlsplit

import httpx

from .schemas import EntitlementPut


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("FSM_LICENSE_URL"))
    parser.add_argument("--allow-http", action="store_true", help="local development only")
    commands = parser.add_subparsers(dest="command", required=True)
    put = commands.add_parser("put", help="create, renew, suspend, revoke or rotate activation key")
    put.add_argument("external_id")
    put.add_argument("--customer", required=True)
    put.add_argument("--expires-at", required=True, help="ISO 8601 timestamp with timezone")
    put.add_argument("--device-limit", required=True, type=int)
    put.add_argument("--status", choices=["active", "suspended", "revoked"], default="active")
    put.add_argument("--revision", required=True, type=int)
    put.add_argument("--rotate-activation-key", action="store_true")
    listing = commands.add_parser("devices", help="list device IDs for support")
    listing.add_argument("external_id")
    release = commands.add_parser(
        "release", help="release lost hardware before activating replacement"
    )
    release.add_argument("external_id")
    release.add_argument("activation_id")
    args = parser.parse_args(argv)
    token = os.environ.get("FSM_LICENSE_INTERNAL_TOKEN", "")
    if len(token) < 32:
        parser.error("set FSM_LICENSE_INTERNAL_TOKEN to the service credential")
    if not args.url:
        parser.error("set FSM_LICENSE_URL or pass --url")
    url = urlsplit(args.url)
    try:
        loopback = ipaddress.ip_address(url.hostname or "").is_loopback
    except ValueError:
        loopback = url.hostname == "localhost"
    if url.scheme != "https" and not (args.allow_http and url.scheme == "http" and loopback):
        parser.error("HTTPS required; --allow-http is only for local development")
    if (
        not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in ("", "/")
    ):
        parser.error("URL must be an origin")
    target = f"{args.url.rstrip('/')}/internal/v1/entitlements/{quote(args.external_id, safe='')}"
    payload = None
    if args.command == "put":
        try:
            payload = EntitlementPut(
                external_customer_id=args.customer,
                expires_at=args.expires_at,
                device_limit=args.device_limit,
                status=args.status,
                revision=args.revision,
                rotate_activation_key=args.rotate_activation_key,
            ).model_dump(mode="json")
        except ValueError as exc:
            parser.error(str(exc))
        method = "PUT"
    elif args.command == "devices":
        method, target = "GET", target + "/activations"
    else:
        method, target = "DELETE", target + "/activations/" + quote(args.activation_id, safe="")
    try:
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            response = client.request(
                method, target, json=payload, headers={"Authorization": f"Bearer {token}"}
            )
        if response.status_code >= 300:
            print(f"Request rejected ({response.status_code}): {response.text}")
            return 1
        print(json.dumps(response.json(), indent=2) if response.content else "Device released.")
        return 0
    except httpx.HTTPError:
        print("License service unreachable.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
