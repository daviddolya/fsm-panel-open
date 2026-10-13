# Online licensing

This is the new application's own licensing system. It does not reproduce or bypass the original application's license service.
An entitlement grants a term and a number of device slots. Payments and customer checkout belong entirely to the external application.

The service uses FastAPI, SQLAlchemy, PostgreSQL and Alembic. Desktop devices use Ed25519 keys protected by Windows user-scope DPAPI.
The desktop requires a fresh online lease after every restart. Leases last at most 60 seconds; renewal runs immediately, then every 30 seconds.

## Run a local demonstration

Run these commands from the repository checkout using its installed Python environment:

```powershell
python -m fsm_license.dev --sqlite .\runtime-data\license-dev.sqlite3
```

The launcher creates the database, applies migrations, generates fresh in-memory service credentials, and listens on `127.0.0.1:8766`.
Its console prints the service token, public key, issuer, audience and key ID needed for this local session.
Keep that console private. Restarting changes its signing key and service token; update the desktop's pinned public key accordingly.
SQLite is only for sequential functional demonstrations. It does not provide the PostgreSQL locking guarantee.

In a second terminal, copy the printed temporary token into the environment and provision a synthetic entitlement:

```powershell
$env:FSM_LICENSE_URL = 'http://127.0.0.1:8766'
$env:FSM_LICENSE_INTERNAL_TOKEN = 'COPY_THE_TEMPORARY_TOKEN'
python -m fsm_license.admin --allow-http put demo-order --customer demo-customer --expires-at 2030-12-31T23:59:59Z --device-limit 2 --revision 1
```

The JSON result includes `activation_key` once. Give that key to the desktop activation command or interface.
Desktop and admin clients accept HTTP only when explicitly enabled and the destination is a loopback address.
Remote connections require HTTPS and normal certificate verification.

To exercise PostgreSQL locally, pass a dedicated development database URL to `fsm_license.dev --database-url` instead of `--sqlite`.

## Deploy the service

Generate independent deployment credentials:

```powershell
python -m fsm_license.keys
```

Copy `.env.example` to the ignored `.env`, then fill its placeholders from the generated values.
Do not commit this file or private key output. Only the public key, issuer, audience, URL and key ID belong in desktop configuration.
Never distribute the service token or signing private key to the desktop.

```powershell
docker compose --env-file .env up --build
```

The Compose service waits for PostgreSQL, applies `alembic upgrade head`, and starts the API.
The port binds only to host loopback. Put a TLS reverse proxy in front of it for remote clients.
Restrict `/internal/` access to your external application's network and provide its separate bearer credential.
Configure request-size limits and rate limiting at that proxy. Forward paths and raw query strings without rewriting signed requests.
The service exposes `/healthz` for process health; this endpoint does not check database readiness.

For a separately managed PostgreSQL installation, supply these environment variables before starting the service:

| Variable | Value |
| --- | --- |
| `FSM_LICENSE_DATABASE_URL` | Dedicated `postgresql+psycopg://...` connection |
| `FSM_LICENSE_INTERNAL_TOKEN` | Cryptographically random service token, at least 32 characters |
| `FSM_LICENSE_SIGNING_KEY` | Base64url encoding of the raw 32-byte Ed25519 private key |
| `FSM_LICENSE_ISSUER` | Stable issuer identifier pinned by desktops |
| `FSM_LICENSE_AUDIENCE` | Desktop audience identifier |
| `FSM_LICENSE_KEY_ID` | Signing key identifier present in the desktop's public-key map |

```powershell
alembic upgrade head
uvicorn fsm_license.app:app_factory --factory --host 127.0.0.1 --port 8766
```

Production startup fails when a required value is missing or still contains a placeholder.
Production configuration rejects SQLite URLs. The SQLite path is only available through explicit application construction or the local launcher.
Use synchronized server and device clocks. A device proof outside the 30-second clock window is rejected.

## Provision, renew, revoke and transfer

The external application calls `PUT /internal/v1/entitlements/{external_id}` with its service bearer credential.
The URL supplies the external entitlement identifier. No payment fields are accepted.

```json
{
  "external_customer_id": "customer-123",
  "expires_at": "2030-12-31T23:59:59Z",
  "device_limit": 2,
  "status": "active",
  "revision": 1,
  "rotate_activation_key": false
}
```

`status` accepts `active`, `suspended` or `revoked`. Timestamps must contain a timezone.
Each changed entitlement requires a larger positive integer revision.
An identical retry at the current revision returns the existing state with `changed: false`.
A smaller revision or different payload at the same revision returns HTTP 409 without changing entitlement state.

Creation issues a random 256-bit activation key. The database stores its SHA-256 hash, never the raw key.
The response returns the raw key only on creation or explicit rotation; idempotent retries never redisclose it.
If the first response is lost, submit a larger revision with `rotate_activation_key: true` to obtain a replacement.
Retain the highest external revision durably in the external application's own transaction/outbox workflow.
Rotation invalidates the previous activation key but preserves existing device activations.

The admin CLI provides the same operations:

```powershell
python -m fsm_license.admin put order-123 --customer customer-123 --expires-at 2030-12-31T23:59:59Z --device-limit 2 --revision 2
python -m fsm_license.admin put order-123 --customer customer-123 --expires-at 2030-12-31T23:59:59Z --device-limit 2 --status revoked --revision 3
python -m fsm_license.admin devices order-123
python -m fsm_license.admin release order-123 ACTIVATION_UUID
```

Set `FSM_LICENSE_URL` to the HTTPS service origin and `FSM_LICENSE_INTERNAL_TOKEN` to its service credential.
The `release` command handles lost hardware without needing that device's private key.
Its API is `DELETE /internal/v1/entitlements/{external_id}/activations/{activation_id}`.
The corresponding GET collection lists activation IDs and release reasons for support.
After release, activate the replacement installation with the current activation key.

Device slots persist until release. A missed heartbeat does not silently free a slot.
Lowering the limit releases the newest activations first, within the entitlement update transaction.
Suspended, revoked, expired or released activations cannot receive another lease.
An already issued lease remains valid for at most its remaining 60-second bound; renewal detects the change sooner.

## Public API and proof of possession

| Method and path | Request | Result |
| --- | --- | --- |
| `POST /v1/activations` | Activation key, installation UUID, base64url device public key | Activation ID and signed lease |
| `POST /v1/leases/renew` | `activation_id` | Fresh signed lease |
| `GET /v1/license/current?activation_id=UUID` | Signed empty body | Current license and fresh lease |
| `DELETE /v1/activations/{activation_id}` | Signed empty body | Idempotent device release, HTTP 204 |

Every public request carries `X-Device-Timestamp`, `X-Device-Nonce`, and `X-Device-Signature`.
Generate a fresh random nonce for every attempt, including retries.
The signature is base64url Ed25519 over these UTF-8 lines, without a trailing newline:

```text
FSM-PROOF-1
METHOD
/path?raw=query
UNIX_SECONDS
NONCE
LOWERCASE_SHA256_HEX_OF_EXACT_BODY_BYTES
```

Renewal and release load the previously bound device key. Public-key knowledge alone cannot authorize them.
The service validates the signature and timestamp, then records the device fingerprint and nonce in the transaction.
Replays of successfully committed requests return HTTP 409. Failed transactions do not allocate devices or change entitlements.
Proof records expire only after their timestamp window is no longer useful; later valid requests clean old records.

Activation with the same installation UUID and key reuses its existing active slot.
The same installation UUID with a different key is rejected until its previous slot is released.
Changing the method, path, query, timestamp, nonce or body invalidates the proof.

Export OpenAPI, including service and device security schemes:

```powershell
python -m fsm_license.openapi .\artifacts\license-openapi.json
```

HTTP 401 means invalid credentials, proof or clock. HTTP 403 means an unavailable entitlement or activation.
HTTP 409 means a revision conflict, reused proof, occupied installation or full device limit.
HTTP 422 means invalid request fields. All responses carry `Cache-Control: no-store`.

## Desktop integration and failure behavior

Construct `LicenseClient` with an HTTPS origin, a `WindowsDpapiDeviceStore`, pinned public keys, issuer and audience.
`activate(key)`, `renew()`, `current()` and `deactivate()` are asynchronous.
Run `run_renewal(stop_event)` on the application's asynchronous worker loop, and close the client during shutdown.
Its HTTP deadline is 10 seconds; redirects are rejected.

The GUI composition layer can adapt the synchronous `LeaseGate.require_valid()` protocol to the coordinator's admission interface.
It returns immutable `LeaseClaims` or raises `LicenseUnavailable` without network I/O.
Use this gate for new operations and monitor it while operations run so the coordinator can stop safely.
Stop, export and reconciliation must remain available without a lease.
The license package deliberately does not call game/process shutdown itself; the coordinator owns safe stopping.

The verifier checks the pinned Ed25519 key, algorithm, issuer, audience, installation, device key, activation and fresh request nonce.
The server bounds lease expiry by the entitlement end date. The client also rejects a lease duration over 60 seconds.
The deadline starts at request dispatch on the monotonic clock, so delayed responses cannot extend admission.
Wall-clock rollback closes the gate. Wall-clock expiry and the monotonic deadline independently close it.
No lease is saved to disk. Restart requires another online proof and signed response.
Any HTTP failure, rejected response, invalid lease or failed secure credential persistence closes the gate immediately.
If the network disappears just after renewal, detection occurs at the next 30-second attempt; the lease still expires within 60 seconds.

The DPAPI file contains the installation ID, private key and activation ID, encrypted together for the current Windows user.
A damaged or undecryptable file is preserved and reported; the client does not silently regenerate it and consume another slot.
Use admin transfer for a lost identity. `MemoryDeviceStore` is ephemeral and intended only for tests or explicit simulations.
A device slot identifies an installation/key pair. This is not hardware attestation, and an open-source local client remains modifiable.

## Persistence, migration and rollback

All PostgreSQL seat mutations lock the entitlement row with `SELECT ... FOR UPDATE`.
Creation first uses `INSERT ... ON CONFLICT DO NOTHING` on the unique external ID, then takes the same row lock.
Activation, release, entitlement revision changes and limit reduction therefore serialize for each entitlement across workers.
The nonce primary key provides persistent uniqueness across service processes.
These guarantees require PostgreSQL. Sequential SQLite tests cannot establish race safety.

Back up the database and signing configuration before deploying migrations.
Use PostgreSQL's `pg_dump` with custom format and restore into a separate test database to verify the backup.
Apply `alembic upgrade head` only after backup verification. The initial migration creates three tables and their indexes.
`alembic downgrade base` deletes the licensing tables; it is not a data-preserving rollback.
For production rollback, restore the verified backup into a separate database and switch the service configuration after validation.
Do not delete a Compose volume to reset production licensing data.

Distribute a new public key to desktops before switching the service's signing key and key ID.
The client's public-key mapping supports a transition period with both keys.
Retain backups of signing material under your deployment secret-management policy.

## Validation and remaining stand work

```powershell
python -m pytest tests/licensing -q
python -m ruff check src/fsm_license src/fsm_panel/licensing tests/licensing alembic
python -m mypy src/fsm_license src/fsm_panel/licensing
```

For real concurrent PostgreSQL validation, set `FSM_TEST_POSTGRES_URL` to a dedicated `postgresql+psycopg://...` test database.
The test user needs schema creation permission. Each test creates and removes only its own random schema.

```powershell
python -m pytest tests/licensing/test_postgres.py -m postgres -q
```

The PostgreSQL suite covers simultaneous seat acquisition, first provisioning, revision ordering and duplicate proof submission.
Without that environment variable, those tests skip explicitly.
The implementation session had no running Docker engine or PostgreSQL endpoint, so real PostgreSQL concurrency remains unverified.
The Windows DPAPI test uses the actual current-user Windows API; other platforms skip that test.
The Docker image build, remote TLS deployment, multiple service processes and a 24-hour desktop soak still require their stand runs.
These checks validate the new system's contract; they do not prove parity with the original application's locked licensing behavior.
