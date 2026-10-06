# GeoSyncAI defect-fix and completion checklist

Baseline: `bec97c915e64607a98d65f9cb4858a12ddc6354c`, clean `main`,
`origin/main` equal after fetch. No `AGENTS.md` found. Preserve existing local
databases/uploads and use disposable services only.

## Reproduced findings and implementation evidence

| Requirement / finding | Current status | Planned fix | Acceptance evidence | Limitations / dependencies |
|---|---|---|---|---|
| Central authorization and restricted feature reads | Fixed in backend with capability-aware membership helpers, restricted-source gates, field references and explicit citizen grants | Complete every protected-resource matrix and browser coverage | Focused limited-role, expiry and Postgres workflow tests pass | Citizen public records remain explicit grants; no name-based access |
| Production service worker private-cache leak | Fixed shell/static allowlist, old-cache purge, bounded account/project-scoped offline store and logout/device-clear flows | Run production multi-session/logout/offline/conflict browser tests | Typecheck and production build pass | Unreachable-device revocation cannot erase local bytes retroactively |
| Job duplicate ownership and cancellation race | Fixed lease/fencing-token ownership, expiry-only reclaim, bounded attempts, heartbeat and authoritative cancellation | Run overlapping Redis workers and kill/retry races | Lease/cancel regression and Postgres-container tests pass | At-most-one valid owner commit, not exactly-once delivery |
| Measurement CRS and units | Fixed projected/geodesic methods, CRS gates, unit conversion, extent/area-of-use checks and lineage metadata | Add trusted-reference and extreme-extent coverage | Geographic rejection and feet-to-metric conversion tests pass | Wide/multi-zone/polar/antimeridian may be gated |
| Independent ground control | Reproduced: checkpoint participates in fit and is called independent | Separate fit/checkpoint sets, rank/conditioning, shared similarity parameters, thresholds, coverage/extrapolation, versioned normalized output | Divergent checkpoint/degenerate controls and transform recovery tests | Source bytes/original coordinates remain immutable |
| Split/merge/shared-edge geometry changes | Server-managed split/merge successors, predecessor retirement, atomic approval, publication lineage and export implemented | Add map editor, shared-edge/holes/rollback and full merge acceptance | One-parent split → validate → publish → export and OGC tests pass | Legal boundary authority remains outside the app |
| Independent publication verification | Reproduced: mutable current manifest is rehashed and can still verify | Persist trusted manifest/output digests, verify expected artifacts/signatures when configured, distinguish source/manifest/output integrity | Altered source/manifest/geometry/missing-artifact/restore tests | Same-database administrator trust boundary remains explicit |
| Typed compliance | Typed operators, ranges, ratios, units, effective dates, applicability, FAR/FSI inputs and explainable results implemented | Finish management UI and broader rule fixtures | Minimum operator and expired-grant regression tests pass | Synthetic rules are screening evidence, not legal certification |
| Grant/assignment expiry and field sync | Expiry/revocation/status checks, scoped idempotency, conflict resolution/resubmission and audit fields implemented | Complete browser lifecycle and reconnect acceptance | Expiry, assignee, event-reuse and offline-conflict tests pass | Server reauthorization is required on reconnection |
| Source registry/dictionary/templates | Existing core; dictionary is displayed but not fully used in suggestions | Apply confirmed bilingual terms/templates to mapping suggestions and replacement-version UI | Schema suggestion/template/version tests | Optional embeddings remain non-required |
| Three-source reconciliation | Existing case storage; acceptance does not yet materialize canonical membership/per-field selections | Connect reviewed case decisions to safe identity/link and explicit selections | Polygon + revenue CSV through publish/lineage | No unsafe transitive identity |
| Field/citizen UI | Basic panels only; assignment lifecycle and case refresh incomplete | Assignment create/revoke/expiry/routing, conflict resolver, grant/case response/status UI, capability-correct initialization | Browser role journeys | No simulated government identity |
| Structured query | Parser exists; ward/date/proximity filters are incomplete and nearby tasks are unfiltered | Typed visible plans, actual filters/radius/results/export and field/citizen scope | Query injection/mutation/permission/filter browser tests | No model-generated SQL |
| Ranker/calibration | Artifact pipeline exists but no real calibrator or inference activation UI | Separate calibrator/test, reliability plots, activation gate and deterministic fallback | Leakage/baseline/learned/calibration tests | Uncalibrated scores never become probabilities |
| Raster/3D | Magic bytes/header-only raster registry and extrusion toggle | Genuine GeoTIFF metadata/CRS/bounds/resolution, bounded tiles/viewer, validated height source/units/reference | Genuine TIFF fixtures and production viewer tests | GDAL/raster service may be required |
| OGC API | Permission checks, stable ordering, bounded pagination, version selection and PostGIS bbox filtering implemented | Add broader resource/permission/conformance coverage | Postgres-container split publication OGC pagination/bbox test passes | Only declared classes are claimed |

## Work order

1. Add migrations/models/capability helpers and reproduce/fix authorization,
   grant/assignment expiry, and scoped data access.
2. Harden service worker and offline storage, then fix job leases/cancellation.
3. Correct measurement and ground-control math and add independent tests.
4. Implement geometry operation/publication/lineage/verification foundations.
5. Implement typed compliance, complete registry/reconciliation/query/ranker/
   raster/OGC workflows and browser controls.
6. Run fresh actual PostGIS/Redis/Celery, production browser, Compose, migration,
   restart, restore, security matrix, and evaluation checks; update docs.

## Continuation rules

- Inspect `git status` before edits; never reset user work or touch existing
  `backend/geosyncai.db`/`backend/storage`.
- Read this file and `WORK_PROGRESS.md` after compaction. Record exact commands,
  disposable service names/ports, changed files, failures and skips.
- Do not count skipped checks as passes. Do not push or publish this task without
  explicit authorization.
- A feature is complete only with backend behavior, usable UI/command, persistence,
  correct permission checks, errors/loading/empty states, and acceptance evidence.

## Verification log

- Audit at `bec97c9`: all nine priority findings reproduced by source inspection;
  replacement implementations and focused tests are now in progress.
- 2026-10-06: `python3 -m pytest backend/tests/test_review_fixes.py backend/tests/test_backend.py backend/tests/test_extended_workflows.py backend/tests/test_migrations.py backend/tests/test_worker.py -q` — **22 passed, 1 skipped**.
- 2026-10-06: `python3 -m pytest backend/tests -q` — **29 passed, 2 failed, 2 skipped**; both failures are Fiona import failures in the system Python 3.14 environment. `test_migrations.py` separately passes.
- 2026-10-06: pinned Docker Compose build/start completed with disposable project `geosyncai-review-20261006`; API health/readiness, frontend shell, worker readiness, and Alembic head `0006_integrity_access_leases` verified. Postgres-container test run excluding the container-path-only migration subprocess test: **32 passed, 1 skipped, 1 deselected**.
- 2026-10-06: `python3 -m compileall -q backend/app migrations/versions`, frontend `npm run typecheck`, and frontend `npm run build` pass. Vite reports the existing large-chunk warning.
- Historical baseline evidence remains in `docs/VERIFICATION.md` and is not fresh
  evidence for this defect-fix task.
