# GeoSyncAI

**SIH26013 — Automated Integration and Intelligent Harmonization of Multi-source
Geospatial Data for Urban Land Record Management**

**Team Git Good · Team ID 151551**

A responsive web application for the **Parcel Evidence Workspace**: preserve
source records, inspect CRS/schema quality, propose identities, review evidence,
select boundaries and attributes, validate a candidate dataset, publish a version,
and export its lineage. The application supports reconciliation; it does not
establish ownership, certify boundaries, or update official government records.

## Linux / WSL startup

Use Python **3.12** and Node **22**. Python packages are pinned in
`backend/requirements.txt`; browser-test dependencies are in `requirements-dev.txt`.
Use Linux-native dependencies in WSL. A Linux-home checkout is faster than `/mnt/c`
or `/mnt/d`; preserve any existing Windows environment instead of reusing its venv.

From the repository root, with [uv](https://docs.astral.sh/uv/) installed:

```bash
uv venv --python 3.12 .venv-wsl
uv pip install --python .venv-wsl/bin/python -r backend/requirements-dev.txt
source .venv-wsl/bin/activate

# Set these in every API/worker/seed terminal, or put them in backend/.env.
export JWT_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export DATABASE_URL="sqlite:///$PWD/backend/geosyncai.db"
export STORAGE_DIR="$PWD/backend/storage"
export PYTHONPATH="$PWD/backend"

python -m app.seed_demo              # optional, explicit demonstration accounts
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

With an installed Python 3.12, `python3.12 -m venv .venv-wsl` and
`.venv-wsl/bin/pip install -r backend/requirements-dev.txt` are equivalent.

In another terminal:

```bash
cd frontend
npm ci
npm run dev -- --host 127.0.0.1
```

Open <http://localhost:5173>. `VITE_API_BASE_URL` defaults to
`http://localhost:8000/api`. All enabled processing and publication actions use
the real API; there is no local mock-processing fallback. Check occupied ports
with `ss -ltnp` before starting services and choose free ports if necessary.

The API applies Alembic migrations on startup. It does not silently fall back to
`create_all` if migration fails. `GET /health` checks the database; `/ready` also
checks the broker when configured. Outside explicitly enabled `DEMO_MODE`, a
JWT secret of at least 32 characters is required. `AUTO_BOOTSTRAP` defaults off.

### Explicit demonstration accounts

`python -m app.seed_demo` creates a project and accounts, **not source datasets**:

| Username / password | Role |
|---|---|
| `viewer` / `viewer` | Read-only |
| `processor` / `processor` | Upload, metadata, processing and validation |
| `reviewer` / `reviewer` | Review and publication |
| `admin` / `admin` | Administration and all project operations |

These credentials are for an isolated demonstration. Use separate provisioned
credentials and secrets for deployment. Global and project permissions are both
checked; owning a project does not turn a processor into a reviewer.

## Docker Compose target

The configuration includes frontend/Nginx, API, PostgreSQL/PostGIS, Redis, Celery
worker, database/upload volumes, migrations and readiness checks.

```bash
cp .env.example .env
# Fill JWT_SECRET and POSTGRES_PASSWORD with random URL-safe values.
# Example value generator: python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
docker compose config
docker compose up --build -d
docker compose run --rm api python -m app.seed_demo
```

Frontend: <http://localhost:5173>; API: <http://localhost:8000>.
Nginx proxies `/api` so the browser build does not depend on a hardcoded API host.
The API and worker share the upload volume. Do not start a second local frontend
on the same port. Docker Desktop's **WSL integration** must be enabled if using it.

**Verification boundary:** Docker was unavailable in the implementation WSL
distribution. The complete container build/start has not been run. Native
PostgreSQL 18.6/PostGIS 3.6.2 and Redis 8.0.5 integration tests passed; Compose
targets PostgreSQL 16/PostGIS 3.4 and Redis 7. CI is configured to test those target
database/broker versions, but no remote CI run is claimed.

## Officer workflow

1. Create/select a project. Upload GeoJSON, CSV (including attribute-only tables),
   GeoPackage or a flat Shapefile ZIP. Supply source organization and capture date.
2. Inspect the hash, quality report and records, including quarantine. Confirm
   unknown CRS using documented evidence; the UI does not guess a default CRS.
3. Confirm field mapping, including identifier semantics and administrative
   context. Leading zeros and suffixes are retained. Saved mappings can be reused.
4. Explicitly select baseline/comparison sources and generate matches. Review
   uncalibrated scores, alternatives and missing evidence. No source pair or
   synthetic data is selected automatically.
5. Review identity proposals. **Accepting identity does not select a boundary.**
   Separately approve geometry and attribute sources for the canonical parcel;
   per-field source overrides and rationale are recorded.
6. For dated changes, first review snapshot identity links. Compare the explicitly
   selected chronological pair. Rejecting a change retains the approved baseline;
   unresolved changes affecting selected parcels block publication.
7. Validate the exact candidate fingerprint and resulting parcel neighborhood.
   Publish and inspect lineage. Unselected records appear in the exclusion report.
8. Export GeoJSON, GeoPackage (selectable CRS), source-to-canonical CSV, quality or
   lineage. Restore a historical version by creating a new traceable version.

Unmatched records can be explicitly selected as standalone baselines from their
record preview. Split/merge candidates abstain and require field verification;
an arbitrary many-to-one merge is never approved by the one-to-one endpoint.

## Five-minute demonstration

For a short demo, use two one-parcel GeoJSON files with the same geometry,
`parcel_id: "0001"` and `village_code: "DEMO"`, dated 2024-01-01 and 2025-01-01.
Sign in as `admin`, create a project, upload both, confirm CRS/mappings, select
the pair, generate a match, accept identity, approve a baseline, validate and
publish. Download the CSV: two source rows should point to one canonical UUID.

Alternatively click **Generate labelled synthetic sources** in Overview. This
explicit action creates a dated 25-parcel pair; boundary differences still require
review. The exact browser acceptance demonstration is automated in `e2e/test_browser.py`.

## Tests and measured evaluation

```bash
# From repository root, in the Linux Python environment:
python -m pytest backend/tests -q
python -m playwright install --with-deps chromium
python -m pytest e2e -q

# Dedicated disposable PostGIS database and Redis broker:
TEST_DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST/TEST_DB \
BROKER_TEST_URL=redis://HOST:6379/15 python -m pytest backend/tests -q
E2E_DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST/TEST_DB \
python -m pytest e2e -q

npm --prefix frontend run typecheck
npm --prefix frontend run build

# Fresh output directories; answer key is kept outside application inputs:
PYTHONPATH=backend python -m app.generate_evaluation_pack --output /tmp/geosyncai-pack --count 1000
PYTHONPATH=backend python -m app.benchmark_evaluation --output /tmp/geosyncai-benchmark --count 1000
```

Tests use new temporary SQLite databases and do not delete existing project data.
PostGIS tests use the explicitly supplied test database; do not point them at
production. Browser tests allocate free ports, run real services, and stop only
their own processes. See [verification results](docs/VERIFICATION.md) and
[requirement status](REQUIREMENTS.md). The synthetic benchmark is not an accuracy
certification or a measurement of manual savings.

## Backup / restore / reset

Stop API and workers before a coordinated database/upload backup. For SQLite,
copy `backend/geosyncai.db` and `backend/storage/` together while stopped. Restore
both into a new directory and point `DATABASE_URL`/`STORAGE_DIR` at those copies.

For Compose:

```bash
docker compose stop api worker
mkdir -p backup
docker compose exec -T postgres pg_dump -U geosyncai -Fc geosyncai > backup/database.dump
docker compose cp api:/app/storage backup/storage
docker compose start api worker

# Restore to a new, empty deployment/database, not over existing user data:
docker compose stop api worker
docker compose exec -T postgres pg_restore -U geosyncai -d geosyncai < backup/database.dump
docker compose cp backup/storage/. api:/app/storage/
docker compose start api worker
```

Reset a local test by choosing a **new database/storage directory**, preserving
the old one. For a deliberately disposable Compose installation only,
`docker compose down -v` deletes its database/upload volumes. Ordinary
`docker compose down` retains them. Migration downgrades do not erase history;
restore a compatible backup instead.

## More detail

- [Architecture and invariants](docs/ARCHITECTURE.md)
- [Backend API](backend/README.md) · [Frontend](frontend/README.md)
- [Persistent continuation checklist](WORK_PROGRESS.md)
