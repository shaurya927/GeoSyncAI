# Security and traffic protection

This project handles source records and account-scoped field evidence. These
controls reduce account abuse, unauthorized access and resource exhaustion. They
do not certify cadastral accuracy or guarantee immunity to attack.

## Account controls

Every protected request checks the current account, role, project permissions,
classification, session version and token revocation. JWTs require expiry, issue
time, not-before, issuer, audience, subject and session claims; only HS256 is
accepted. Sessions expire after 60 minutes by default. Existing JWTs without the
new claims are deliberately invalid after this upgrade: sign in again.

Connected sign-out revokes that token. Changing a passphrase or account access
revokes all previous sessions. Administrators can create accounts, edit access,
disable accounts and revoke sessions on the **Security** page. Account creation
does not grant project access. Self-disable/demotion and removal of the last
active administrator are rejected. Changes are audited without passwords/tokens.

New passphrases require 15–128 characters. PBKDF2-HMAC-SHA256 uses a random salt
and 600,000 iterations; older valid hashes are upgraded on successful login.
Unknown and inactive accounts return the same login error. Login budgets bound
password-hashing work. See the
[OWASP password storage guidance](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html).

Ordinary browser tokens use tab session storage; field accounts retain their token
in local storage for bounded offline reload. Browser storage is **not an encrypted
vault**. Offline devices cannot learn server revocation until reconnect. Sign-out
keeps account-scoped field drafts, but removes credentials and cached sessions;
sync requires server authorization again. Use trusted devices and OS disk encryption.

## Traffic limits

| Control | Default | Scope |
|---|---:|---|
| API requests | 600/minute | Authenticated account, otherwise client IP |
| Login requests | 20/minute | Client IP, even if a bearer token is supplied |
| Login account budget | 10/15 minutes | Normalized account name |
| Heavy POST requests | 60/minute | Account/client identity |
| Active requests | 64 | Each API process |
| Active heavy requests | 2 | Each API process |
| Queued/running jobs | 10 | Project, serialized database capacity check |
| JSON/body size | 2 MiB | Non-upload request, actual streamed byte count |
| Uploaded file | 250 MiB | File; multipart request allows 64 KiB framing |
| Source records | 50,000 | One ingested dataset |
| Body read/total time | 30/180 seconds | API request body |
| Nginx API traffic | 10/second, burst 40 | Client IP |
| Nginx login traffic | 20/minute, burst 5 | Client IP |
| Nginx connections | 20 | Client IP |

These are admission budgets, **not measured concurrent-user capacity**. Requests
over budget receive 429; saturated admission or unavailable protection receives
503. `Retry-After` tells clients when to retry. Do not raise limits without testing
representative datasets on the intended host. Heavy native upload work runs in
FastAPI's thread pool so it does not block the HTTP event loop.

Redis provides atomic rate counters shared across API workers. It stores HMAC
identity fingerprints rather than raw account names/IPs. Production requires
Redis; Redis errors fail closed. Local SQLite demonstrations use bounded in-memory
counters for one process. Concurrency limits remain per process. The Security page
shows aggregate HTTP status counters and configured limits; Redis metrics expire
after 24 hours without updates. Counters are not a complete monitoring system.

Set the documented environment variables in `.env.example` / `backend/.env.example`.
Compose forwards the rate, concurrency, body/file, record and job limits. Nginx
limits are separately configured in `frontend/nginx.conf`. Shared NAT users share
the proxy IP budget: tune it only with capacity evidence and a real proxy setup.
No automatic client retry loop or external load test is performed.

## Deployment and upgrades

1. Back up the database and immutable upload volume before migrations. Migration
   `0009_security_sessions` adds account session versions and token revocations;
   existing accounts and source data are retained. Never reset/delete volumes to
   fix an upgrade. Old root-owned upload volumes need ownership changed to UID/GID
   10001 on **that backed-up storage volume only** before the non-root API/worker
   can write. Do not recursively change host folders or database-volume ownership.
2. Use random separate JWT/database secrets. The repository and delivery ZIP
   exclude private environments, `.local-demo/`, uploads and databases. The local
   launcher is for loopback demonstrations; it is not a public application server.
3. Provision a real administrator interactively, without putting passwords in
   shell history, command arguments or source:

   ```bash
   docker compose run --rm api python -m app.provision_admin --username operator
   ```

   For a local configured backend, use `python -m app.provision_admin --username
   operator` from `backend/`. Existing usernames are never overwritten. On an
   existing demonstration, use a separate real administrator to disable or change
   all active known demonstration passwords before enabling production mode.
4. Set `PRODUCTION_MODE=true`, `AUTO_BOOTSTRAP=false`, `DEMO_MODE=false`, a real
   `SECURITY_REDIS_URL`, `RATE_LIMIT_ENABLED=true`, explicit `ALLOWED_HOSTS`, and
   HTTPS `CORS_ORIGINS` (or `[]` for same-origin only). Production refuses demo
   bootstrap, disabled rate protection, missing shared storage, repetitive JWT
   secrets, HTTP CORS origins and active known demo passwords. Do not seed a
   production database. The base Compose profile remains an isolated demo until
   these operator settings are supplied.
5. Put frontend traffic behind a maintained HTTPS reverse proxy/load balancer.
   Add TLS/HSTS there. Ports 5173/8000 bind to loopback; PostgreSQL/Redis have no
   published ports. Use a separately provisioned least-privilege database runtime
   role and migration role for public deployment; the bundled PostGIS demonstration
   database role owns its database and is **not** that production separation.
6. Do not expose the API container directly. It disables Uvicorn proxy-header
   inference and trusts only `X-GeoSyncAI-Client-IP` from the exact Nginx container
   address (`172.30.91.2/32`). Nginx overwrites that header and X-Forwarded-For;
   arbitrary client-forwarded headers cannot select a new rate identity. When
   adding an outer proxy, configure Nginx real-IP processing for that exact trusted
   hop and test spoofing; otherwise clients intentionally share the proxy's IP
   budget. Never trust `0.0.0.0/0`. If subnet 172.30.91.0/24 conflicts on your host,
   change the subnet, both fixed container IPs and trusted /32 together.
7. API/worker containers run as UID 10001, drop capabilities, forbid privilege
   escalation, use a read-only root filesystem and bounded temporary storage,
   CPU/memory. Maintain container OS packages, Redis/PostGIS and reverse proxy
   images. Application dependency audit passes do not audit the container OS.

CORS and Host allowlists have no wildcard. API documentation is off by default;
`API_DOCS_ENABLED=true` can enable it on a controlled local backend. For Vite on a
different port, explicitly add that origin to backend `CORS_ORIGINS`. Production
responses apply CSP, anti-framing, nosniff, referrer and permissions policies.
Sensitive API responses are `no-store`. Application security logs, metrics and
Nginx access logs omit credentials, query parameters and raw client IPs; Uvicorn
access logs are disabled in Compose/the launcher. Infrastructure error logs can
contain connection metadata; restrict their access and retention. Use request IDs
to correlate application rejections.

Archives reject traversal, symlinks, encryption, unsafe/non-Shapefile members,
oversized expansion and excessive compression ratios. These checks and record
caps are resource controls, not antivirus scanning or isolation of native parsers.
Raw source bytes remain retained for provenance, including rejected input; enforce
disk quotas/retention and backups in the hosting environment.

## Verification and remaining scope

Security regressions exercise real session revocation, account permissions,
restricted origins/hosts, oversized/streamed bodies, slow requests, admission
recovery, forwarded-header spoofing, login budgets and project queue limits. Redis
CI checks one atomic budget through two clients. Browser tests use the actual API
for account creation, passphrase change, disabling users, sign-out and offline
retention. Compose smoke checks headers, Redis-backed controls, non-root processes
and an isolated proxy burst. External map tiles are fixtures in automated tests.

Pinned dependency audits run in Acceptance CI; CodeQL analyzes Python and
JavaScript/TypeScript. Passing checks establish these tested behaviors, not the
absence of every vulnerability. Exact run evidence is in `docs/FINAL_ACCEPTANCE.md`.

Public hosting still needs TLS, backup/restore drills, storage/retention monitoring,
capacity testing and upstream DDoS protection. MFA/SSO, an encrypted offline vault,
external malware scanning and an independent penetration test are not implemented.
Report security issues privately to the repository owner; do not post credentials,
personal land records or exploit payloads in a public issue.
