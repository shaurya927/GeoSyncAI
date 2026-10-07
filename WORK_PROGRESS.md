# GeoSyncAI submission implementation — active work

Team Git Good · 151551 · SIH26013. Baseline `7a93faec449064ad684ceade18d009d44352fe55`;
working tree changes are local and uncommitted. Remote fetch succeeded over SSH.
No commit, push, deployment, or submission is authorized for this task.

## Protected resources

Existing `backend/geosyncai.db`, `backend/storage`, uploads, and user volumes are
not fixtures and must not be deleted. Disposable Compose projects and temporary
archives created for verification were removed after their checks.

## Current implementation

- Explicit global capability roles and project-role intersections gate departmental,
  fieldwork, and citizen capabilities; expiry/revocation and wrong-project tests
  are covered.
- Geometry changes enforce project ownership, canonical source resolution,
  optimistic approval claims, revision/hash checks, and split → publish → merge →
  publish → rollback lineage/digest behavior.
- Citizen grants bind frozen published versions; citizen values are read from the
  frozen publication and include version provenance.
- Reviewed ranker activation is persisted through migration `0008_ranker_activation`;
  active inference remains explicitly uncalibrated.
- Field assignment, citizen grant/case, reconciliation, compliance, query,
  ground-control, raster registry/preview, OGC, publication, rollback, export, and
  independent artifact-verification APIs are connected to the frontend.
- The UI has geometry/query/compliance/assignment/grant/ground-control/raster/ranker
  panels. The production workflow now has a Playwright script covering upload →
  CRS/mapping → matching → review → selection → validation → publication → export.
- Genuine GeoTIFF inspection uses pinned rasterio and produces bounded PNG previews;
  no surveyed volumetric or photogrammetric claim is made.

## Fresh evidence

- Full local system-Python suite: **33 passed, 2 failed, 3 skipped**. The failures
  and raster skip are Fiona/rasterio availability limits in Python 3.14.
- Focused local defect/workflow suite: **15 passed, 1 skipped** for the same local
  raster dependency boundary.
- Pinned Python 3.12/PostGIS/Redis/Celery Compose suite through migration 0008:
  **38 passed, 0 skipped**, 2 warnings.
- Compileall, Ruff F-only lint, frontend typecheck/build, and `git diff --check`
  passed. Vite still reports its large-chunk warning.
- Limited production Nginx/Chromium smoke passed admin login, new views, no page
  errors, and 390px responsive width.
- Latest production Playwright workflow passed upload → publish → GeoJSON export →
  refresh with one feature, two lineage sources, no page errors, and no overflow.
- API restart followed by production Nginx/Chromium smoke passed.
- Separate-stack restore passed: restored API health/frontend, project/version IDs,
  exported GeoJSON SHA-256, and raw-upload SHA-256 matched the source.
- Synthetic 1,000-parcel benchmark completed in 10.8792s; metrics and limitations
  are recorded in `docs/FINAL_ACCEPTANCE.md`.

## Remaining acceptance work

1. Run the broader Python officer browser workflow against the latest production
   image, including the same-publication API restart and download assertions.
2. Exercise the complete field/PWA browser matrix: offline reload, scoped cache,
   account/project switching, token expiry, logout with drafts, revoked assignment,
   quota/storage errors, reconnect conflicts, and citizen grant/case flows.
3. Extend the map editor from bounded rectangle/envelope helpers to authoritative
   vertex/snap/split/union interactions, and add direct OGC/query/compliance/raster
   browser regressions where useful.
4. Keep final acceptance, requirements, checklist, README, and verification counts
   synchronized with exact commands and honest limitations.
5. Inspect final status, remove any ignored temporary credentials, and leave all
   implementation changes uncommitted and unpushed.

## Verification commands

```bash
python3 -m pytest backend/tests -q
python3 -m compileall -q backend/app migrations/versions
PYTHONPATH=/tmp/omnirush/ruff python3 -m ruff check backend/app migrations/versions --select F
npm --prefix frontend run typecheck
npm --prefix frontend run build
git diff --check
```

The authoritative geospatial/backend command uses a fresh pinned Compose project,
copies `backend/tests` into `/app/tests`, sets `TEST_DATABASE_URL` and
`BROKER_TEST_URL=redis://redis:6379/15`, and requires zero skips/deselections.
