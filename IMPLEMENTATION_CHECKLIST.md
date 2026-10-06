# GeoSyncAI implementation and continuation — Phases A–D

Baseline audited: `ab15c1d6214ddce1b188ba04cb931b1b0c2efd80`, clean `main`,
`origin/main` equal after `git fetch origin`. No applicable AGENTS.md found.
The baseline core is present; verification documents describe historical runs.
Local databases/uploads exist and are user data: use disposable databases only.

| Requirement | Current status | Implementation sequence | Acceptance evidence | Dependencies / limitations |
|---|---|---|---|---|
| Source registry, metadata, bilingual mappings | Implemented core | A1 additive revisioned metadata, Hindi/English dictionary, summaries and departmental templates | `test_source_registry_three_source_reconciliation_and_measurement` passed | Embeddings optional; never infer missing facts |
| Multi-source and field provenance/policy | Implemented core | A2 explicit independent anchor comparisons, competing values, per-field provenance and policy revision gates | Three-source GeoJSON/CSV focused test passed | No universal authority hierarchy |
| Split/merge and neighborhood editing | Partial, reviewable | A3 reviewed changesets with revision, measurements, conservation, predecessor/successor UUIDs; publication application still gated | API/schema implemented; direct split/merge publication test pending | Preserve holes; atomic publication still required |
| Measurement and ground control | Implemented core | A4 extent-aware projected measurement and paired controls/checkpoints/approval | Focused measurement test passed; ground-control path pending | Wide/polar/antimeridian may be explicitly gated |
| Supervised ranker/calibration | Implemented core | B1 grouped reproducible logistic artifact and reliability report; deterministic default | Ranker focused test passed | Calibration quality remains data-dependent |
| Officer collaboration | Partial | B2 live evidence/review/history plus field assignments and map 3D toggle | Focused field assignment path passed | Saved views/swipe/bulk routing remain |
| Jobs/incremental/dashboard | Implemented baseline | B3 visible stage/heartbeat/warnings/cancel plus project accounting dashboard | Existing worker tests and job subset passed | Incremental invalidation/dashboard browser proof pending |
| Offline field PWA | Implemented core | C1 installable shell, bounded assignments, GPS/photo/note drafts, idempotent revision-aware sync | UI code and API conflict path implemented; browser offline acceptance pending | Browser storage/retention limitations |
| Hindi/English query | Implemented core | C2 whitelisted read-only plans/results, project scope and limits | Focused query test passed | LLM optional; no model SQL |
| 3D/raster | Partial | C3 valid height-source/vertical-reference extrusion and hash-checked attributed raster registry | Frontend build and raster schema compile passed | Configured GDAL tile service/imagery suggestions remain |
| Compliance | Implemented core | C4 versioned approved rules, units/missing data and calculations | Focused pass evaluation passed | Demo rules synthetic, no legal determination |
| Citizen grants/disputes | Implemented core | C5 explicit grants, allowlisted snapshots, audited cases | API path implemented; dedicated citizen browser proof pending | No name-based access or simulated government identity |
| Audit verification/read API | Implemented core | D1 canonical manifest/source hashes and paginated/bbox OGC read paths | API code and schema compile passed | OGC declaration only for tested classes; signatures conditional |
| Roles/resources/security | Partial | D2 added field/citizen roles, restricted source/raster gates, bounded feature reads | Focused isolation paths passed | Full threat-model/dependency/restore/performance audit pending |
| Evaluation/integrated delivery | Partial | D3 preserve historical pack, add focused acceptance and update evidence | 3 additive tests, baseline subsets and build passed | Fresh PostGIS/Compose, 1,000/10,000 reruns pending |

## Active work

Repository audit and first additive A/B/C/D slice are complete. Continue with
reviewed split/merge publication application, ground-control transformation,
bounded raster tiles, assignment/citizen browser acceptance, and fresh isolated
PostGIS/Compose evidence. Preserve existing routes/tests and map fix. New work is
not authorized for external publishing/pushing.

## Verification / failures

- 2026-10-06: Python compileall, focused additive tests (3 passed), selected
  baseline/worker tests (15 passed, 1 skipped), migration test (1 passed),
  frontend typecheck/build and diff check passed. The current system Python had
  21 passed and 2 optional-Fiona import failures; a full pinned disposable
  container suite then passed **26 tests** in 14.82 s with PostGIS and real
  Redis/Celery delivery. A fresh Compose build reached migration
  `0005_raster_assets`; API/worker/PostGIS/Redis/Nginx health, seeded login,
  three uploads, reconciliation, dashboard and OGC collections passed.

## Continuation rules

- Inspect `git status` before edits; do not reset user changes or touch existing
  `backend/geosyncai.db`/`backend/storage`.
- Read this file plus WORK_PROGRESS.md after compaction. Record exact commands,
  disposable-service names/ports, files changed and remaining failing tests here.
- Completion requires usable frontend/backend, persistence, permissions, errors,
  meaningful acceptance evidence. Partial implementations stay marked partial.
