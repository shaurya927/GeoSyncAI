# Verification — 2026-10-05

## Defect-fix verification — 2026-10-06

This record covers the uncommitted defect-fix work after baseline commit
`bec97c915e64607a98d65f9cb4858a12ddc6354c`. It used fresh temporary SQLite data
and a disposable Docker Compose project; existing `backend/geosyncai.db` and
`backend/storage` were not used.

| Check | Actual result |
|---|---|
| Focused regression and defect-fix tests | **22 passed, 1 skipped**, 1 warning |
| Full system-Python backend suite | **29 passed, 2 failed, 2 skipped**; the two failures are Fiona imports unavailable in Python 3.14 |
| Migration test in system environment | **1 passed** |
| Postgres-container backend tests, excluding the migration subprocess test whose copied `/app/tests` path cannot see repository `migrations` | **32 passed, 1 skipped, 1 deselected**, 1 warning |
| Python compileall | Passed |
| Frontend typecheck | Passed |
| Frontend production build | Passed; existing Vite large-chunk warning remains |
| Disposable Compose build/start | Passed with API, PostGIS, Redis, Celery worker and Nginx frontend healthy |
| Compose API health/readiness and database migration | Passed; Alembic head `0006_integrity_access_leases` |
| Published split, lineage, OGC bbox/pagination verification | Passed in focused tests, including Postgres-container execution |
| Independent CLI and source-tamper verification | Passed in the current local focused suite |

The container migration test itself passes in the system environment. The
container-only deselection is a test harness path issue, not an application
failure. The remaining skipped test is the existing broker-dependent worker
case. Browser logout/offline multi-session journeys, independent-stack restore,
full overlapping-worker races and the complete PostGIS acceptance matrix remain
separate follow-up checks.

## Additive Phase A-D implementation verification — 2026-10-06

The working tree now includes migration `0005_raster_assets` and the additive
metadata, reconciliation, geometry-review, ranker, fieldwork, query, compliance,
citizen, OGC and raster-registry APIs. These checks were run against a fresh
temporary SQLite database; they do not count as the historical PostGIS/Compose
acceptance below.

| Check | Actual result |
|---|---|
| Python compileall (`backend/app`, migrations) | Passed |
| Focused additive workflow tests | **3 passed**, 1.06 s |
| Baseline backend subset plus worker tests | **15 passed**, 1 skipped, 6.20 s |
| Full backend suite in disposable pinned container | **26 passed**, 3 warnings, 14.82 s; PostGIS and real Redis/Celery duplicate-delivery path included |
| Migration upgrade/restart through `0005_raster_assets` | **1 passed**, 3.59 s |
| Frontend TypeScript typecheck | Passed |
| Frontend production build | Passed; existing MapLibre chunk-size warning remains |
| `git diff --check` | Passed |
| Full backend suite in current system environment | **21 passed**, 2 failed because optional Fiona is not installed in that environment; 2 skipped |

The focused tests cover three-source GeoJSON/CSV evidence, metadata hash guards,
Hindi mapping terms, metric measurement, grouped supervised ranker artifacts,
explicit field assignments, idempotent/revision-bounded field evidence,
compliance missing-input gates, read-only query planning and OGC collection
access. The optional Fiona failures are system-Python dependency failures; the
pinned Docker/runtime requirements include Fiona and the full container suite
passed.

## Executed

| Check | Actual result |
|---|---|
| Backend suite with real PostgreSQL/PostGIS and Redis/Celery worker | **23 passed**, 11.45 s |
| Real Chromium E2E using PostGIS API, download and API restart | **1 passed**, 19.95 s |
| Frontend TypeScript | Passed |
| Frontend production build | Passed; MapLibre chunk-size warning remains |
| Python undefined/unused-name lint (`ruff --select F`) | Passed |
| `git diff --check` | Checked after documentation updates |
| Fresh additive Compose build/start | Passed; API, worker, PostGIS 16/PostGIS 3.4, Redis 7 and Nginx frontend healthy; Alembic reached `0005_raster_assets` |
| Fresh Compose API smoke for additive slice | Passed; seeded demo, login, project, three uploads including CSV, reconciliation with 3 independent features, dashboard and OGC collections |
| Full Docker Compose smoke | Passed; fresh-volume startup and service checks for PostgreSQL, Redis, API, Celery and Nginx |
| Chromium against Compose/Nginx | Passed in 13.973 s; uploads, CRS/mapping, Celery matching, review, selection, validation, publication, all five exports, API restart and mobile-width check |
| Separate-stack database/upload restore | Passed; browser download, publication/lineage, canonical UUID, completed job receipt and both raw uploads matched the source |

The backend suite includes the original-schema migration/restart check, required
revision handling, namespaces/distant parcels, split-candidate abstention, source
preservation, unknown/implausible CRS and correction, canonical publication,
rejected/deferred boundary behavior, candidate neighborhood overlaps, validation
invalidation, raw format round-trips, GeoPackage/GeoJSON/CSV reopening, rollback,
project role restrictions, transactional failure/replay, native PostGIS storage/
GiST and real Celery duplicate delivery.

The browser test drives real forms and endpoints: login → project → two uploads →
CRS → mapping → pair selection → match → evidence → identity decision → explicit
baseline → validation → publication → download. It then restarts the API, retrieves
the version, checks for browser JavaScript errors and checks narrow-screen overflow.

### Environment

- Linux WSL2, x86_64; Python 3.12.15 in an isolated Linux environment.
- Native PostgreSQL **18.6**, PostGIS **3.6.2**, GEOS **3.14.1**, PROJ **9.7.1**.
- Redis **8.0.5** and Celery **5.6.3**; real broker/worker process.
- Chromium through Playwright **1.63.0**; Node **22.23.3**, Vite **7.3.6**.
- Test database and services created under `/tmp/omnirush`; existing application
  databases/storage were not used by these integration runs.
- Docker Desktop Linux engine **29.8.1**, Compose **5.5.1**, was used through the Windows Docker
  CLI because the WSL-native Docker command was not integrated. The isolated
  Compose runs used PostgreSQL **16.4/PostGIS 3.4.3**, Redis **7.4.11**, Python **3.12**,
  Celery **5.6.3**, Fiona **1.10.1/GDAL 3.9.2** and Nginx **1.27.5**. The container
  browser probe used Playwright **1.63.0** and Chromium **153.0.8010.12** from WSL.
- GitHub [Acceptance run 37339083449](https://github.com/shaurya927/GeoSyncAI/actions/runs/37339083449)
  passed on commit `6663c0f`. That workflow runs native API/browser tests with
  PostGIS/Redis service containers; the full Compose probes below ran locally.

### Full Compose browser and restore probes

The initial `geosyncai-verify` smoke checked `/health`, `/ready`, Nginx proxying,
seeded demo accounts, a real 25-record Celery matching job, API restart and retained
proposals. Continued verification used independent `geosyncai-browser-verify` and
`geosyncai-restored-verify` projects, each with its own database/upload volumes.

- Fresh-volume startup exposed a readiness race: the Unix-socket `pg_isready`
  probe passed during the PostGIS image's temporary initialization server, before
  the API could connect over TCP. `docker-compose.yml` now probes `127.0.0.1`;
  startup was repeated successfully with new verification volumes.
- The browser then exposed HTTP 503 on GeoPackage export: importing Fiona in
  `python:3.12-slim` failed because `libexpat.so.1` was missing. `backend/Dockerfile`
  now installs `libexpat1`; API/worker images rebuilt and Fiona import passed.
- Chromium used the production frontend at port 5173 with its same-origin `/api`
  proxy. Two unknown-CRS one-parcel GeoJSON uploads were confirmed and mapped,
  matched by the actual Celery worker, linked by review, explicitly selected,
  validated and published. All five downloads succeeded. GeoJSON had one stable
  parcel and two lineage sources; CSV had two rows with the same parcel UUID;
  quality reported valid; GeoPackage contained one feature with EPSG:32643.
- Restarting the API retained the publication and identical GeoJSON. The browser
  reported zero JavaScript errors and no horizontal overflow at 390 px width.
- Source API/worker containers were stopped before capturing a custom-format
  `pg_dump` and the upload directory as a tar stream. A separate target stack was
  started on ports 18000/15173. Its test database was prepared from `template0`,
  because the stock PostGIS image already initializes extension schemas.
  `pg_restore --no-owner --single-transaction --exit-on-error` and upload restore
  completed before the target API/worker started.
- Through the restored Nginx API and browser, the published GeoJSON/lineage,
  version/parcel UUIDs, source links, baseline selection and succeeded job receipt
  matched. Both raw downloads were byte-identical and matched their SHA-256 hashes.
  Browser login and version download also passed in the restored stack.

Both continued-verification stacks and their volumes/networks were removed after
the checks. Temporary probe scripts/reports are under `/tmp/omnirush`; existing
application databases/uploads were not used. The additional Docker fixes and this
expanded verification record are included in the repository.

## Synthetic evaluation

Latest measured output is preserved in `evaluation-2026-10-04.json`; reproduce it
with the root README benchmark command and a fresh output directory.

The pack has 1,000 reference parcels, 1,000 alternate records, 1,000/1,001 dated
snapshot records, a 1,000-row attribute-only revenue table and separate unknown/
wrong-CRS inputs. It includes reused namespace IDs, leading zeros, a valid hole,
invalid geometry, duplicate/contained polygons, a missing counterpart, ambiguity,
a split-like case, a genuine displacement and a below-tolerance displacement.
The answer key is separate; application matching never reads it.

| Measurement | Observed |
|---|---:|
| Total benchmark time (including simulated review calls) | 14.0621 s |
| Candidate recall against labeled counterpart sets | 99.6994% |
| Top-match precision | 99.8996% |
| Top-match recall | 99.6994% |
| Score ≥0.98 non-ambiguous subset precision / coverage | 100% / 99.3% |
| Explicit abstention fraction | 0.6% |
| Human review required before publication | 100% |
| Three labeled conflict-case precision / recall | 100% / 100% |
| Geometry-change precision / recall / F1 | 1 / 1 / 1 |
| Three selected publication features: lineage completeness | 100% |

These are **small, synthetic, scoped measurements**. Geometry-change metrics cover
one positive and one below-tolerance case; they are not a general vector-change F1.
Conflict metrics cover three labeled examples. Publication completeness covers
three selected features, not every generated parcel. The high-score subset is
uncalibrated and is not auto-approved by the application. Schema-suggestion accuracy,
manual effort reduction and real-world cadastral accuracy have not been measured.
