"""License API. Run with uvicorn fsm_license.app:app_factory --factory."""

import hashlib
import hmac
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import Depends, FastAPI, HTTPException, Path, Request, Response
from fastapi.openapi.utils import get_openapi
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import Settings
from .crypto import (
    activation_key_hash,
    b64decode,
    b64encode,
    canonical_json,
    key_fingerprint,
    proof_message,
    sign_lease,
)
from .database import Activation, Database, Entitlement, ProofNonce
from .schemas import (
    ActivationRequest,
    EntitlementPut,
    EntitlementView,
    LicenseView,
    RenewRequest,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def aware(value: datetime) -> datetime:
    # SQLite drops timezone metadata; PostgreSQL retains it.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def create_app(
    settings: Settings, database: Database | None = None, *, clock: Callable = utcnow
) -> FastAPI:
    class LicenseAPI(FastAPI):
        def openapi(self):
            return documented_openapi()

    db = database or Database(settings.database_url)
    app = LicenseAPI(title="FSM License Service", version="1.0.0")
    app.state.database = db

    def documented_openapi():
        if app.openapi_schema is None:
            schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
            schema.setdefault("components", {})["securitySchemes"] = {
                "ServiceBearer": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "Separate service credential; never distributed to desktops.",
                },
                "DeviceTimestamp": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-Device-Timestamp",
                    "description": "Unix seconds; must be within 30 seconds of server time.",
                },
                "DeviceNonce": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-Device-Nonce",
                    "description": "Fresh 32-100 character base64url nonce for each request.",
                },
                "DeviceSignature": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-Device-Signature",
                    "description": (
                        "Base64url Ed25519 signature over UTF-8 FSM-PROOF-1, method, path plus "
                        "raw query, timestamp, nonce, and hex SHA-256 of exact request bytes, "
                        "separated by newline characters, with no trailing newline."
                    ),
                },
            }
            for path, operations in schema["paths"].items():
                for operation in operations.values():
                    if path.startswith("/internal/"):
                        operation["security"] = [{"ServiceBearer": []}]
                    elif path.startswith("/v1/"):
                        operation["security"] = [
                            {
                                "DeviceTimestamp": [],
                                "DeviceNonce": [],
                                "DeviceSignature": [],
                            }
                        ]
            app.openapi_schema = schema
        return app.openapi_schema

    @app.middleware("http")
    async def private_responses(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    def service_auth(request: Request) -> None:
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied.encode(), f"Bearer {settings.internal_token}".encode()):
            raise HTTPException(401, "invalid service credential")

    async def raw_body(request: Request) -> bytes:
        return await request.body()

    def active_count(session: Session, entitlement_id: str) -> int:
        return (
            session.scalar(
                select(func.count())
                .select_from(Activation)
                .where(
                    Activation.entitlement_id == entitlement_id, Activation.released_at.is_(None)
                )
            )
            or 0
        )

    def view(session: Session, item: Entitlement, *, key=None, changed=False) -> EntitlementView:
        return EntitlementView(
            external_id=item.external_id,
            external_customer_id=item.external_customer_id,
            expires_at=aware(item.expires_at),
            device_limit=item.device_limit,
            status=item.status,
            revision=item.revision,
            active_devices=active_count(session, item.id),
            activation_key=key,
            changed=changed,
        )

    def available(item: Entitlement, now: datetime) -> None:
        if item.status != "active":
            raise HTTPException(403, f"entitlement {item.status}")
        if aware(item.expires_at) <= now:
            raise HTTPException(403, "entitlement expired")

    def consume_proof(
        session: Session, request: Request, body: bytes, public_key: str, now: datetime
    ) -> str:
        timestamp = request.headers.get("x-device-timestamp", "")
        nonce = request.headers.get("x-device-nonce", "")
        signature = request.headers.get("x-device-signature", "")
        if not re.fullmatch(r"[0-9]{1,12}", timestamp):
            raise HTTPException(401, "invalid device proof timestamp")
        if abs(now.timestamp() - int(timestamp)) > settings.proof_skew_seconds:
            raise HTTPException(401, "device proof expired or device clock incorrect")
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,100}", nonce):
            raise HTTPException(401, "invalid device proof nonce")
        target = request.url.path
        if request.url.query:
            target += "?" + request.url.query
        try:
            key_bytes = b64decode(public_key)
            if len(key_bytes) != 32 or b64encode(key_bytes) != public_key:
                raise ValueError("noncanonical key")
            Ed25519PublicKey.from_public_bytes(key_bytes).verify(
                b64decode(signature), proof_message(request.method, target, timestamp, nonce, body)
            )
        except ValueError, InvalidSignature:
            raise HTTPException(401, "invalid device proof") from None
        session.execute(delete(ProofNonce).where(ProofNonce.expires_at < now))
        session.add(
            ProofNonce(
                device_fingerprint=key_fingerprint(key_bytes),
                nonce=nonce,
                expires_at=now + timedelta(seconds=2 * settings.proof_skew_seconds + 1),
            )
        )
        try:
            session.flush()
        except IntegrityError:
            raise HTTPException(409, "device proof already used") from None
        return nonce

    def locked_activation(session: Session, activation_id: UUID) -> tuple[Entitlement, Activation]:
        entitlement_id = session.scalar(
            select(Activation.entitlement_id).where(Activation.id == str(activation_id))
        )
        if entitlement_id is None:
            raise HTTPException(404, "activation not found")
        # All activation and entitlement writers take this same lock, in this order.
        item = session.scalar(
            select(Entitlement).where(Entitlement.id == entitlement_id).with_for_update()
        )
        activation = session.get(Activation, str(activation_id), populate_existing=True)
        if item is None or activation is None:
            raise HTTPException(404, "activation not found")
        return item, activation

    def lease_view(item: Entitlement, activation: Activation, now: datetime, nonce: str):
        available(item, now)
        if activation.released_at is not None:
            raise HTTPException(403, "activation released")
        issued = int(now.timestamp())
        expires = min(issued + settings.lease_seconds, int(aware(item.expires_at).timestamp()))
        if expires <= issued:
            raise HTTPException(403, "entitlement expired")
        claims = {
            "v": 1,
            "iss": settings.issuer,
            "aud": settings.audience,
            "sub": item.external_id,
            "activation_id": activation.id,
            "installation_id": activation.installation_id,
            "device_key_sha256": key_fingerprint(b64decode(activation.device_public_key)),
            "device_limit": item.device_limit,
            "revision": item.revision,
            "iat": issued,
            "exp": expires,
            "jti": str(uuid4()),
            "request_nonce": nonce,
        }
        return LicenseView(
            activation_id=UUID(activation.id),
            external_id=item.external_id,
            expires_at=aware(item.expires_at),
            device_limit=item.device_limit,
            revision=item.revision,
            lease=sign_lease(claims, settings.signing_key, settings.key_id),
        )

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.put(
        "/internal/v1/entitlements/{external_id}",
        response_model=EntitlementView,
        dependencies=[Depends(service_auth)],
    )
    def put_entitlement(
        payload: EntitlementPut, external_id: str = Path(min_length=1, max_length=200)
    ):
        now = clock()
        digest = hashlib.sha256(canonical_json(payload.model_dump(mode="json"))).hexdigest()
        key = secrets.token_urlsafe(32)
        values = dict(
            id=str(uuid4()),
            external_id=external_id,
            external_customer_id=payload.external_customer_id,
            expires_at=payload.expires_at,
            device_limit=payload.device_limit,
            status=payload.status,
            revision=payload.revision,
            revision_digest=digest,
            activation_key_hash=activation_key_hash(key),
            created_at=now,
            updated_at=now,
        )
        with db.sessions.begin() as session:
            # The unique external_id also serializes concurrent first provisioning.
            insert = pg_insert if db.engine.dialect.name == "postgresql" else sqlite_insert
            created = session.scalar(
                insert(Entitlement)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["external_id"])
                .returning(Entitlement.id)
            )
            item = session.execute(
                select(Entitlement).where(Entitlement.external_id == external_id).with_for_update()
            ).scalar_one()
            if created:
                return view(session, item, key=key, changed=True)
            if payload.revision < item.revision:
                raise HTTPException(409, "stale entitlement revision")
            if payload.revision == item.revision:
                if not hmac.compare_digest(digest, item.revision_digest):
                    raise HTTPException(409, "conflicting payload for entitlement revision")
                return view(session, item)
            item.external_customer_id = payload.external_customer_id
            item.expires_at = payload.expires_at
            item.device_limit = payload.device_limit
            item.status = payload.status
            item.revision = payload.revision
            item.revision_digest = digest
            item.updated_at = now
            if payload.rotate_activation_key:
                item.activation_key_hash = activation_key_hash(key)
            # A limit decrease releases newest seats immediately, within this same lock.
            active = list(
                session.scalars(
                    select(Activation)
                    .where(Activation.entitlement_id == item.id, Activation.released_at.is_(None))
                    .order_by(Activation.created_at, Activation.id)
                )
            )
            for activation in active[payload.device_limit :]:
                activation.released_at = now
                activation.release_reason = "device_limit_reduced"
            session.flush()
            return view(
                session, item, key=key if payload.rotate_activation_key else None, changed=True
            )

    @app.post("/v1/activations", response_model=LicenseView, status_code=201)
    def activate(payload: ActivationRequest, request: Request, body: bytes = Depends(raw_body)):
        now = clock()
        with db.sessions.begin() as session:
            item = session.scalar(
                select(Entitlement)
                .where(
                    Entitlement.activation_key_hash == activation_key_hash(payload.activation_key)
                )
                .with_for_update()
            )
            if item is None:
                raise HTTPException(401, "invalid activation credential")
            nonce = consume_proof(session, request, body, payload.device_public_key, now)
            available(item, now)
            activation = session.scalar(
                select(Activation).where(
                    Activation.entitlement_id == item.id,
                    Activation.installation_id == str(payload.installation_id),
                    Activation.released_at.is_(None),
                )
            )
            if activation is not None and activation.device_public_key != payload.device_public_key:
                raise HTTPException(409, "installation already bound to another device key")
            if activation is None:
                if active_count(session, item.id) >= item.device_limit:
                    raise HTTPException(409, "device limit reached")
                activation = Activation(
                    id=str(uuid4()),
                    entitlement_id=item.id,
                    installation_id=str(payload.installation_id),
                    device_public_key=payload.device_public_key,
                    created_at=now,
                )
                session.add(activation)
                session.flush()
            return lease_view(item, activation, now, nonce)

    @app.get(
        "/internal/v1/entitlements/{external_id}/activations",
        dependencies=[Depends(service_auth)],
    )
    def list_devices(external_id: str):
        with db.sessions() as session:
            item = session.scalar(select(Entitlement).where(Entitlement.external_id == external_id))
            if item is None:
                raise HTTPException(404, "entitlement not found")
            return [
                {
                    "activation_id": device.id,
                    "installation_id": device.installation_id,
                    "created_at": aware(device.created_at),
                    "released_at": aware(device.released_at) if device.released_at else None,
                    "release_reason": device.release_reason,
                }
                for device in session.scalars(
                    select(Activation)
                    .where(Activation.entitlement_id == item.id)
                    .order_by(Activation.created_at, Activation.id)
                )
            ]

    @app.delete(
        "/internal/v1/entitlements/{external_id}/activations/{activation_id}",
        status_code=204,
        dependencies=[Depends(service_auth)],
    )
    def admin_release(external_id: str, activation_id: UUID):
        with db.sessions.begin() as session:
            item, activation = locked_activation(session, activation_id)
            if item.external_id != external_id:
                raise HTTPException(404, "activation not found for entitlement")
            if activation.released_at is None:
                activation.released_at = clock()
                activation.release_reason = "administrator_request"
        return Response(status_code=204)

    def issue_for_activation(activation_id: UUID, request: Request, body: bytes):
        now = clock()
        with db.sessions.begin() as session:
            item, activation = locked_activation(session, activation_id)
            nonce = consume_proof(session, request, body, activation.device_public_key, now)
            return lease_view(item, activation, now, nonce)

    @app.post("/v1/leases/renew", response_model=LicenseView)
    def renew(payload: RenewRequest, request: Request, body: bytes = Depends(raw_body)):
        return issue_for_activation(payload.activation_id, request, body)

    @app.get("/v1/license/current", response_model=LicenseView)
    def current(activation_id: UUID, request: Request, body: bytes = Depends(raw_body)):
        return issue_for_activation(activation_id, request, body)

    @app.delete("/v1/activations/{activation_id}", status_code=204)
    def deactivate(activation_id: UUID, request: Request, body: bytes = Depends(raw_body)):
        now = clock()
        with db.sessions.begin() as session:
            _, activation = locked_activation(session, activation_id)
            consume_proof(session, request, body, activation.device_public_key, now)
            if activation.released_at is None:
                activation.released_at = now
                activation.release_reason = "device_request"
        return Response(status_code=204)

    return app


def app_factory() -> FastAPI:
    return create_app(Settings.from_env())
