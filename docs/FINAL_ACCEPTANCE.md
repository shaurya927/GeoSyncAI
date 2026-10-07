# Final acceptance — 7 October 2026

## Delivered revision

Local branch: `codex/submission-fixes-20261007`, based on remote `main`
`10eb59aed7b4cf583e3169f92437c773c315be11`. This acceptance record was captured
locally before GitHub publication, which the user subsequently authorized.
No public deployment or SIH submission was performed. The delivery ZIP contains
editable source and the verified production frontend. Its package manifest records
SHA-256 hashes of every included file.

Existing backend databases, storage directories and user volumes were preserved.
Backend/browser checks used disposable databases and storage. The local preview
uses its own `.local-demo/` directory, which is excluded from the ZIP.

## Repaired behavior

- Every structured query checks project/account capabilities before reading source
  tables. Departmental classifications apply to query rows, registries, evidence,
  canonical parcels and processing inputs. Ward/date/bbox/metre-radius filters
  actually filter the results before pagination. Nearby field queries expose only
  current bounded assignments, not departmental or unpublished citizen records.
- Ground-control fitting and independent checkpoint residuals are reported in
  metres for both geographic and projected/feet target coordinates. Finite
  coordinate ranges, source CRS consistency, checkpoint threshold, source hash,
  revision and validated control coverage gate approval. Extrapolation is blocked.
  A linked aligned dataset preserves original bytes/coordinates/declared CRS and
  inherits its schema mapping.
- A previously verified field account can reload the production PWA offline and
  reopen its scoped assignments, references and drafts. The lease is bounded by
  eight hours and token/assignment expiry. Sync rechecks the server account and
  project permission. Revocation does not fall back to cached authority. Quota
  failure retains the form; sign-out locks access and retains unsynced evidence
  for the same account. Explicit resubmission records a rationale/current revision.
- True polygon split/intersection and union preserve concavity, holes and lineage.
  The editor supports drawn cuts, pointer-dragged vertices, metric-tolerance
  snapping, coupled coincident shared-edge vertices, coordinate editing, undo,
  measured previews, neighbor impact checks and approve/reject/defer review.
  Coverage, overlap, stale-boundary and changed-policy checks remain on the server.
- Three-source reconciliation has a comparison table and explicit boundary,
  default attribute and per-field source choices. Identity acceptance and baseline
  approval remain separate choices. Accepted/deferred/rejected decisions are
  revisioned; a reviewer rationale is required. Reviewed labels/training/activation
  are also connected to the optional uncalibrated ranker UI.
- Two-band GeoTIFF previews now encode valid RGB PNG rows. Compliance rejects
  incompatible length/area units, nonfinite measurements and malformed area ratios.
  Dataset pages are loaded explicitly beyond the former first 500 records.

## Fresh checks on this working tree

Environment: Windows, Python 3.12.14, Node 24.19.0, Playwright Chromium 153,
SQLite/local processing, installed Fiona/rasterio/Shapely/PROJ dependencies.
Recommended deployment versions remain Python 3.12 and Node 22.

| Check | Result |
|---|---|
| `python -m pytest backend/tests -q` | **49 passed, 2 skipped**; 30.52 s |
| `python -m pytest backend/tests/test_submission_repairs.py -q` | **13 passed**; includes independent new failure regressions |
| `python -m pytest e2e/test_browser.py e2e/test_submission_repairs.py -q` | **3 passed**; 31.74 s |
| Final targeted production browser rerun after reconnect-state correction | **2 passed**; 17.04 s; includes pointer drag/undo and drawn cut |
| `npm --prefix frontend run typecheck` | Passed |
| `VITE_API_BASE_URL=/api npm --prefix frontend run build` | Passed; prebuilt assets included |
| `python -m compileall -q backend/app migrations/versions scripts` | Passed |
| `python -m ruff check backend/app migrations/versions scripts --select F` | Passed |
| `git diff --check` | Passed |
| Real local launcher / HTTP API / seeded demo | Health/frontend HTTP 200; 3 demo datasets, stop/restart persistence and raw source hashes verified |

The officer browser test covers upload, CRS/mapping, identity review, explicit
baseline, validation, publication, exports, refresh, map/tile behavior and 390px
viewport. The production repair tests cover offline first reload, queued retention,
reconnect/revision resubmission, revocation, account switching, expiry, storage quota,
three-source choices, map-drawn concave split, reviewed merge, dragged vertex/undo,
coordinate correction, defer/approve, aligned registry refresh and bounded query UI.
Application APIs are real HTTP calls; no mock processing response was used. The
external basemap provider is replaced with raster tile fixtures in its browser test.

## Service boundary and claims

Two backend tests were skipped because native PostGIS and Redis/Celery services
were not available on this Windows host. This is a local SQLite acceptance result,
not a fresh PostGIS/Redis acceptance claim. The GitHub workflow runs those service
checks and now also runs the production repair browser tests. Run that workflow
on the uploaded revision before a production release. Earlier main-branch CI and
container reports do not certify these new local edits.

The build retains a large MapLibre JavaScript chunk warning (about 1.37 MB before
gzip). It is a performance warning, not a compilation failure. Browser storage is
not an encrypted vault; the bounded offline lease cannot learn server revocation
until reconnect. Snap coordinates are a UI aid; authoritative topology/measurements
use the server analysis CRS.

This is a submission prototype with actual implemented workflows. Synthetic
fixtures/metrics are not measured cadastral accuracy or effort savings. Matching
and optional learned ranking are uncalibrated scores, not correctness probabilities.
Compliance is screening assistance. Citizen access is explicit frozen grants.
GNN research, government identity/records integrations, blockchain anchoring,
photogrammetry, surveyed volumetric twins and capability-based AR are not claimed
as implemented production integrations.

## Basemap correction after provider verification

The user reported an actual CARTO "API KEY REQUIRED" watermark on 7 October 2026.
The previous browser suite replaced external tiles with PNG fixtures, so its pass
verified map rendering/error recovery but did not establish anonymous CARTO service
availability. CARTO's current documentation confirms that a basemap key is required.

The default now uses standard OpenStreetMap tiles without a key. Supplying
`VITE_CARTO_BASEMAP_KEY` selects CARTO's light raster tiles with a URL-encoded key.
The setting reaches local builds and Docker/Compose builds. Attribution stays
visible, and status says "Basemap tiles received" with the provider name rather
than implying the provider's image content was validated. No tile prefetch or
offline basemap download is implemented. README documents provider configuration
and the OpenStreetMap tile usage policy.

Fresh local checks for this correction:

- Frontend typecheck/build and `git diff --check` passed.
- `pytest e2e/test_browser.py e2e/test_submission_repairs.py -q`: **4 passed**,
  40.45 s. The officer workflow now tests both the key-free default and an explicitly
  configured CARTO key, including reserved-character encoding, visible attribution,
  tile failure/retry, overlay selection, publication and responsive layout. External
  tiles remain fixtures in automated viewport tests; no real CARTO key is supplied.
- One identified, non-bulk request to the live OpenStreetMap world tile returned
  HTTP 200, a 256×256 PNG, and normal caching headers. Visual inspection confirmed
  that this real response has no API-key watermark.
- The earlier repaired source revision `ba86665` passed the full GitHub Acceptance
  workflow, including PostGIS/Redis, production repair browser tests, Compose startup
  and Nginx smoke: https://github.com/shaurya927/GeoSyncAI/actions/runs/37611592002.
  The basemap correction receives its own acceptance run when pushed.

## Security and traffic hardening — 7 October 2026

Added current-account session checks, complete JWT claims, connected token logout,
session invalidation after passphrase/access changes, 600,000-round password hashes
with legacy upgrade, administrator account controls and a live traffic panel.
Rate budgets, actual streamed body caps, timeouts, admission limits and serialized
per-project queue capacity now bound work. Redis counters are atomic across API
workers; native upload parsing runs outside the event loop. Restricted origins/hosts,
CSP/headers, loopback ports, trusted proxy identity and non-root container restrictions
protect the browser/deployment boundary. Archive and record caps were strengthened.

Updated MapLibre to patched 6.13.0 and configured its Vite worker explicitly in both
development/production. Updated pytest to 9.0.3 for its reported advisory. New account
APIs/UI do not grant implicit project access. Migration 0009 preserves source records
and existing accounts; old JWTs require a fresh login. See `SECURITY.md` for operational
settings and the offline/hosting boundaries.

Fresh local results on the security implementation:

- Backend suite: **69 passed, 3 skipped**, 54.18 s. Skips require local PostGIS,
  Redis/Celery and Redis atomic-counter services; these run in service CI.
- Real HTTP browser journeys: **5 passed**, 63.73 s. Includes both basemap configurations,
  normal geospatial publication, production offline/geometry workflows and a new
  account creation/passphrase/disable/logout/session-revocation journey under CSP.
- `npm audit --audit-level=high`: **0 known vulnerabilities**.
- `pip-audit -r backend/requirements-dev.txt --no-deps --disable-pip`: **no known
  vulnerabilities** in the pinned application/test requirements at check time.
- Frontend build/typecheck, Ruff F and diff whitespace checks passed. The MapLibre
  bundle retains the previously disclosed size warning.

Acceptance CI now also verifies non-root API/worker identities, production Nginx
headers, Redis-backed UI, token logout and a bounded burst against its disposable
stack. Separate CodeQL analysis covers Python and JavaScript/TypeScript. These new
service checks are pending the security revision's GitHub run; local evidence does
not substitute for that result. The prior basemap revision `54e0191` passed
Acceptance: https://github.com/shaurya927/GeoSyncAI/actions/runs/37621512066.

These changes are tested protection controls, not a penetration-test certificate
or a measured user-capacity claim. Public deployment still requires HTTPS, protected
backups, storage monitoring, least-privilege database provisioning, host capacity
testing and upstream DDoS protection. Offline storage is not encrypted and cannot
learn revocation until reconnect. No public deployment or SIH submission occurred.
