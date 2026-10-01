# GeoSyncAI

GeoSyncAI is a browser-based officer workspace for explainable, reviewable
harmonization of land datasets. It is an SIH26013 prototype: synthetic data is
clearly labelled and is not evidence of cadastral accuracy, legal ownership, or
government certification.

## Run locally with SQLite

Prerequisites: Python 3.11+ and Node.js 20.19+.

```powershell
# Terminal 1 — API
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# Terminal 2 — browser application
cd frontend
Copy-Item .env.example .env
npm install
npm run dev
```

Open <http://localhost:5173>. The default API URL is
`http://localhost:8000/api`. The application is responsive browser UI; no
Android/iOS application is required.

## Run with PostgreSQL/PostGIS

```powershell
docker compose up --build
```

Run the Vite frontend in a second terminal as above. The Compose API is at
<http://localhost:8000> and stores structured geometry in PostGIS while retaining
portable GeoJSON geometry and immutable raw uploads.

## Demonstration accounts

The local bootstrap seeds accounts whose passwords equal their usernames:

| Username | Password | Role | Demonstration use |
|---|---|---|---|
| `viewer` | `viewer` | Viewer | Read-only project and export access |
| `processor` | `processor` | Processor | Upload, validate, normalize, match, topology, and change jobs |
| `reviewer` | `reviewer` | Reviewer | Officer decisions, validation, and publication |
| `admin` | `admin` | Admin | Project and membership administration |

Change all credentials and `JWT_SECRET` before any real deployment.

## Five-minute browser demonstration

1. Sign in as `processor` and open the seeded **Synthetic demonstration** project.
2. Open **Datasets**. Upload a GeoJSON or CSV; inspect record count, SHA-256
   hash-backed raw preservation, schema fields, duplicate IDs, geometry issues,
   and CRS status. An unknown CRS blocks spatial evidence.
3. Click **Run processing**. The API creates/uses the deterministic synthetic
   pair, computes identifier, overlap, area, and centroid evidence, and records
   topology/change results. MapLibre renders normalized source features.
4. Open **Review queue** and select an evidence card. Inspect alternatives,
   source values, conflicts, and the explicit **uncalibrated rule score**. This
   score is not a probability. Sign in as `reviewer` for decisions in a real
   deployment; boundary changes require reviewer approval and later validation.
5. Open **Change alerts** for dated vector differences, then **Versions & history**.
   Run validation, publish only when blocking review/topology checks are clear,
   and use the export menu for GeoJSON, CSV crosswalk, or lineage manifest.

## Checks

```powershell
python -m pytest backend/tests -q
cd frontend; npm run typecheck; npm run build
```

Performance targets in the solution document are not presented as achieved
benchmarks.

## Project documents

- `REQUIREMENTS.md` — required MVP and roadmap status matrix.
- `backend/README.md` — API, persistence, jobs, and deployment details.
- `frontend/README.md` — browser frontend and API contract details.
