# Requirement status

Status refers to this working tree, not an SIH certification. **Tested** means the
named acceptance path ran; it is not proof of every production edge case.

| Capability | Status | Evidence / remaining boundary |
|---|---|---|
| Responsive web workspace, real API | Tested | Chromium/PostGIS workflow plus mobile-width check; no mock fallback |
| Login, actual current user, project selection/create | Tested | Browser workflow; JWT roles and memberships |
| Dataset uploads and immutable originals | Tested | GeoJSON, CSV, SHP ZIP, GeoPackage; bytes/hash retained |
| Archive/resource validation | Implemented | Upload, expanded-size/member-count, path/symlink/ratio checks; not a fuzzing claim |
| Record quality and quarantine inspection | Tested | Invalid geometry retained in previews/topology; no silent normalization discard |
| Dataset versions/metadata | Implemented | Parent UUID, organization/date/hash/classification/accuracy/provenance/namespace/version fields; raw bytes remain immutable and metadata corrections are hash-guarded |
| CRS correction and publication gates | Tested | Impossible coordinates rejected; correction preserves raw input; reviewed sources use new versions |
| Metric analysis | Tested locally | Local UTM, strict PROJ, operation/grid/software lineage; wide-area/polar policy remains limited |
| Polygon/MultiPolygon display | Implemented | Full geometry retained; no first-polygon truncation or origin-square fallback |
| Confirmed, versioned, reusable schema mapping | Tested core | Mapping affects canonical identifier/namespace semantics; templates reuse within project |
| Candidate ranking and administrative context | Tested | Metric STRtree, semantic ID/context, IoU/distance/area/boundary evidence, alternatives |
| Shared-number/distant/competing candidates | Tested | Abstention and one-to-one membership checks |
| Split/merge handling | Tested core, editor connected | Server-generated successors, approved-geometry resolver, project-scoped transactional decisions, area/displacement gates, split→publish→merge→publish and rollback integrity; map editor uses bounded draft helpers and remains a review aid |
| Topology | Tested core | Invalid, duplicate, containment/overlap; explicit coverage gaps implemented |
| Shared-boundary reconstruction | Partial | Reviewed shared-edge changesets and coherent measurement gates; no automatic general boundary repair engine |
| Source precedence | Implemented | Explicit geometry/default attribute source and per-field reviewer overrides; no universal source hierarchy |
| Review state machine and concurrency | Tested | Mandatory expected revision, atomic stale-review rejection, role caps |
| Stable canonical parcel identities | Tested | Accepted source membership; separate baseline approval; stable UUID across publications |
| Boundary rejection/defer policy | Tested | Reject retains baseline; pending/deferred selected changes block |
| Resulting candidate/neighborhood validation | Tested | Exact fingerprint and current policy; overlap checks on assembled canonical geometries |
| Immutable versions, lineage, exclusions | Tested | Included sources/hashes/mappings/CRS/matches/reviews/selections; unselected reasons |
| Dated identity-linked vector changes | Tested core | Explicit chronological pair, metric tolerance; duplicates/splits abstain; missing does not imply demolition |
| Durable jobs | Tested | Rollback of stage side effects, retry and duplicate delivery; actual Redis/Celery worker |
| Job telemetry | Implemented baseline | Visible stage, heartbeat, warnings, cancellation request, timestamps and committed result counts; cancellation is transactional and does not claim partial effects |
| GeoJSON/GeoPackage/CSV/quality/lineage exports | Tested core | Reopened geometry/CRS/JSON attributes; CSV one row per source link |
| Traceable rollback | Tested | New version, old snapshot retained |
| Migrations and restart persistence | Tested | Original-schema additive upgrade through `0005_raster_assets`; browser retrieves publication after API restart |
| PostgreSQL/PostGIS native geometry/GiST | Tested | Native PostgreSQL 18.6 / PostGIS 3.6.2 integration |
| Full Compose deployment | Tested | Fresh-volume startup; Chromium/Nginx upload-through-publication; real Celery job; all five exports; API restart; separate-stack database/upload restore verified through browser/API |
| Secure defaults and opt-in seed | Implemented | Required JWT secret outside demo, explicit seeding, pinned Python/Node dependencies |
| Synthetic evaluation pack | Generated/measured | ~1,000 parcels, separate answer key, checksums/vertices/dependencies/results in docs |
| Source registry, explicit multi-source evidence and bilingual dictionary | Implemented core | Hash-guarded metadata UI, revisioned Hindi/English terms/templates, three-source acceptance materializes links/selections when explicitly requested, competing values remain visible; no ownership adjudication |
| Supervised ranker and calibration artifacts | Tested core | Group-aware labeled logistic ranker, held-out validation activation gate, real inference routing when activated, deterministic fallback, uncalibrated-score labeling and reliability metrics; no probability claim without a calibrator |
| Offline assigned fieldwork PWA | Tested production reload/sync | Installable shell, bounded assignments, GPS/photo/note queue, idempotent sync and revision-conflict draft retention; browser storage limits remain |
| Structured Hindi/English read-only queries | Tested authorization/filters | Whitelisted parser/plans for conflicts, missing links, dated changes and nearby tasks; no arbitrary SQL or mutation |
| Height-aware 3D display and permissioned raster registry | Implemented/tested metadata preview | Genuine GeoTIFF/rasterio CRS/bounds/transform/dimensions/bands/resolution/nodata/hash inspection, bounded PNG preview, classification gates and validated-height-only extrusion; tiled reprojection/terrain remains roadmap |
| Compliance rules and citizen access | Implemented core | Versioned rule inputs/formulas, insufficient-information gates, explicit field grants and audited cases; demo rules are not legal determinations |
| Independent lineage verification and OGC API Features | Implemented/tested core | Canonical manifest/output/source-byte verification including rollback, standalone CLI, collection/item/bbox/version/pagination access and truthful conformance; same-database administrators are not excluded |
| Accuracy/effort objectives | Targets, not generally achieved claims | Scoped synthetic metrics only; schema-suggestion accuracy/manual savings and real cadastral accuracy are not measured |

## Actual verification — local delivery, 7 October 2026

- Fresh full backend suite: **49 passed, 2 skipped**. The skips require unavailable
  native PostGIS and Redis/Celery services. This Windows acceptance used Python 3.12,
  SQLite and real installed geospatial dependencies.
- Browser acceptance: **3 passed** (officer workflow plus two production repair
  workflows). Final targeted production rerun after reconnect-state correction:
  **2 passed**, including actual pointer drag/undo and a map-drawn cut.
- Frontend typecheck/build, Python compilation, Ruff F-only checks and diff-check
  passed. The MapLibre chunk-size performance warning remains.
- Local production launcher: real HTTP health/frontend and authenticated demo
  registry verified. Its data is isolated from existing backend databases/uploads.
- Full service-stack acceptance is configured in `.github/workflows/acceptance.yml`
  and must be run on the uploaded revision; older main-branch container/CI evidence
  is historical, not proof of the new working tree.
- Synthetic evaluation results in `docs/VERIFICATION.md` remain historical scoped
  fixture measurements and were not reclassified as real cadastral accuracy.
- Detailed current commands, behavior and limitations: `docs/FINAL_ACCEPTANCE.md`.

## Roadmap / unavailable

GNN, imagery-change detection, CAD/scans/point clouds/RINEX, government/Aadhaar/
DigiLocker/NAPIX integrations, blockchain and capability-detected AR remain
conditional integrations. Raster tile serving requires a configured GDAL/raster
service; the registry does not fabricate raster metadata. No legal compliance,
government approval or real-world accuracy claim is made.
