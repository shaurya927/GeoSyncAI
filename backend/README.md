# GeoSyncAI backend

GeoSyncAI is a FastAPI prototype for explainable land-data harmonization. It
keeps uploaded bytes unchanged, stores normalized source features separately,
and makes uncertain matches and meaningful changes explicit review targets.

## Local run (SQLite)

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

The default database is `sqlite:///./geosyncai.db` and raw files are kept under
`./storage`. Set `DATABASE_URL` and `STORAGE_DIR` in `.env` to override them.
The service creates tables on startup and seeds these demo users (passwords are
the same as the usernames):

| username | password | role |
|---|---|---|
| viewer | viewer | viewer |
| processor | processor | processor |
| reviewer | reviewer | reviewer |
| admin | admin | admin |

Change all credentials and `JWT_SECRET` before any real deployment. Synthetic
data is labelled and is not evidence of cadastral accuracy.

## PostgreSQL/PostGIS

```powershell
docker compose up --build
```

The compose API uses `postgresql+psycopg://...` and a PostGIS 3.4 database.
The application retains GeoJSON-compatible geometry JSON for portability and,
when the configured URL is PostgreSQL, enables PostGIS and stores normalized
EPSG:4326 geometry in native GiST-indexed geometry columns through GeoAlchemy2.

## Workflow endpoints

- `POST /api/auth/token`, `GET /api/auth/me`
- Project and membership APIs under `/api/projects`
- Register or upload GeoJSON/CSV under `/api/projects/{id}/datasets`
- `POST /api/projects/{id}/bootstrap-synthetic` for deterministic demo parcels
- `POST /api/projects/{id}/match` for explainable rule-based proposals (including unmatched and ambiguous outcomes)
- `POST /api/projects/{id}/topology/{dataset_id}` and `POST /api/projects/{id}/changes/detect`
- `POST /api/projects/{id}/reviews/{match|change|conflict}/{target_id}` for reviewer decisions
- `POST /api/projects/{id}/validate` to validate accepted changes and open topology errors
- `POST /api/projects/{id}/publish` and `GET /api/projects/{id}/versions/{id}/export?format=geojson|csv|lineage`
- `POST /api/projects/{id}/jobs` plus `GET /api/projects/{id}/jobs/{id}` for durable job receipts. Without
  `CELERY_BROKER_URL`, FastAPI background tasks execute jobs locally; with a
  broker, the Celery seam can be wired to a worker.

For Celery, point `CELERY_BROKER_URL` at a supported broker and run:

```powershell
celery -A app.tasks.celery_app worker --loglevel=INFO
```

Uploads accept GeoJSON and CSV. CSV geometry can be GeoJSON, WKT, or longitude /
latitude columns. Original upload bytes are content-addressed by SHA-256 and
never replaced by normalized records. Unknown CRS is retained as an explicit
warning and the dataset enters `needs_crs_review`.

## Tests

From the repository root:

```powershell
python -m pytest backend/tests -q
```

Tests use an isolated SQLite database and cover project authorization, raw
upload preservation, and the invariant that unapproved boundary changes block
publication.
