# Backend

FastAPI + SQLAlchemy; SQLite locally or PostgreSQL/PostGIS. Use the exact WSL,
Compose, seeding and test commands in the root README. Python 3.12 is the verified
runtime; Fiona wheels avoid requiring a system GDAL build in that environment.

## Main routes

All `/api/projects/{project_id}/...` routes require membership and check write/
review permissions as applicable. Downloads have the same authorization checks.

| Route | Purpose |
|---|---|
| `POST /api/auth/token`, `GET /api/auth/me` | Login/current user |
| `GET/POST /api/projects` | Scoped projects/create |
| `POST .../members` | Admin membership management |
| `GET/POST .../policy`, `GET .../history` | Versioned processing policy/audit history |
| `POST .../datasets/upload` | Multipart file, organization, capture date, declared CRS, optional parent dataset UUID |
| `GET .../datasets`, `GET .../datasets/{id}/features` | Quality and previews including quarantine |
| `GET .../datasets/{id}/raw` | Original bytes |
| `POST .../datasets/{id}/confirm-crs` | `{crs,reason}`; reprocess original geometry |
| `GET/POST .../datasets/{id}/mapping` | Canonical-field → source-field mapping; confirmation/versioning |
| `GET .../mapping-templates` | Reusable confirmed mappings within the project |
| `POST .../match`, `.../topology/{id}`, `.../changes/detect` | Synchronous diagnostic stage routes |
| `POST .../jobs?job_type=match\|topology\|change_detection` | Durable processing, input/config hashes, automatic idempotency |
| `GET .../jobs/{id}`, `POST .../jobs/{id}/retry` | Receipt/retry |
| `POST .../reviews/{match\|change\|conflict}/{id}` | Required `{decision,rationale,expected_revision}` |
| `GET .../parcels` | Stable identities, source memberships and current baseline selections |
| `POST .../parcels/{id}/selection` | Explicit geometry/attribute source selection and optional per-field overrides |
| `POST .../features/{id}/baseline` | Explicit standalone baseline for an unmatched source |
| `POST .../validate`, `POST .../publish` | Exact-candidate validation and immutable publication |
| `GET .../versions`, `POST .../versions/{id}/rollback` | History/restore as new version |
| `GET .../versions/{id}/export?format=...` | `geojson`, `gpkg`, `csv`, `quality`, `lineage` |
| `GET .../exports/{format}` | Latest-version export shortcut |

GeoPackage accepts `output_crs=EPSG:32643` (or another supported CRS). GeoJSON
always uses EPSG:4326 longitude/latitude. CSV has one row per source-to-canonical
link. GeoPackage stores arbitrary source attributes and lineage in JSON text
columns plus the stable parcel UUID and native geometry.

## States and transaction boundaries

`proposed` → `under_review` → `accepted` / `rejected` /
`needs_field_verification`. Direct decisions from proposed are allowed.
Deferred/field-verification items can return to review. Final decisions require
new evidence for another revision. A stale expected revision returns HTTP 409.

Accepted identities materialize source links; a separate reviewer selection
controls the baseline. Validation does not silently approve proposals. Its hash
covers candidate features, exclusions, mappings, inputs, policy and decisions.
Publication requires the matching valid report. Source CRS/mapping changes
invalidate unreviewed proposals; reviewed sources require a new dataset version.

Each worker stage holds a conditional database row lock. Side effects and the
succeeded receipt commit together. Failure rolls back effects before saving a
failed receipt. Duplicate delivery of a successful job does no work. Interrupted
transactions remain recoverable. In this prototype running state is transactional
and may appear queued to other sessions until commit; no fabricated progress
percentages are displayed. API startup redispatches queued/interrupted receipts.

Alembic revisions are in `migrations/`; the original schema upgrade is additive.
Historical versions remain snapshots. The audit log is application history, not
legally certified immutability.
