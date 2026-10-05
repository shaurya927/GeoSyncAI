# Verification — 2026-10-05

## Executed

| Check | Actual result |
|---|---|
| Backend suite with real PostgreSQL/PostGIS and Redis/Celery worker | **23 passed**, 11.45 s |
| Real Chromium E2E using PostGIS API, download and API restart | **1 passed**, 19.95 s |
| Frontend TypeScript | Passed |
| Frontend production build | Passed; MapLibre chunk-size warning remains |
| Python undefined/unused-name lint (`ruff --select F`) | Passed |
| `git diff --check` | Checked after documentation updates |
| Full Docker Compose smoke | Passed; isolated Docker Desktop project, all five services healthy, proxied API workflow and worker job completed |
| Compose restart and backup/restore | Passed; API restart retained 25 proposals, disposable PostgreSQL and upload-volume round-trips matched |

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
- Docker Desktop Linux engine **29.8.1** was started through the Windows Docker
  CLI because the WSL-native Docker command was not integrated. The isolated
  Compose project used PostgreSQL **16/PostGIS 3.4**, Redis **7**, API, Celery
  worker and Nginx frontend. It was named `geosyncai-verify`; its containers and
  volumes were disposable and were not application data.
- The smoke path checked `/health` and `/ready`, Nginx `/api` routing, seeded the
  explicit demo accounts, generated the synthetic pair, submitted a real 25-record
  match job to Celery, confirmed success, restarted the API, and retrieved all 25
  persisted proposals. A custom-format PostgreSQL dump restored to a disposable
  database with migration revision `0003_attribute_sources`; uploaded raw files
  were copied out and back into a disposable storage restore directory with matching
  SHA-256 values.

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
