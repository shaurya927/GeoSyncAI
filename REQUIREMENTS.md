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
| Dataset versions/metadata | Implemented/partial | Parent UUID, organization/date/hash/classification/accuracy fields; full metadata editing UI remains limited |
| CRS correction and publication gates | Tested | Impossible coordinates rejected; correction preserves raw input; reviewed sources use new versions |
| Metric analysis | Tested locally | Local UTM, strict PROJ, operation/grid/software lineage; wide-area/polar policy remains limited |
| Polygon/MultiPolygon display | Implemented | Full geometry retained; no first-polygon truncation or origin-square fallback |
| Confirmed, versioned, reusable schema mapping | Tested core | Mapping affects canonical identifier/namespace semantics; templates reuse within project |
| Candidate ranking and administrative context | Tested | Metric STRtree, semantic ID/context, IoU/distance/area/boundary evidence, alternatives |
| Shared-number/distant/competing candidates | Tested | Abstention and one-to-one membership checks |
| Split/merge handling | Partial | Detected/routed to field verification; approved split/merge editing is not implemented |
| Topology | Tested core | Invalid, duplicate, containment/overlap; explicit coverage gaps implemented |
| Shared-boundary reconstruction | Partial | Overlap/gap signals; no general boundary repair engine |
| Source precedence | Implemented | Explicit geometry/default attribute source and per-field reviewer overrides; no universal source hierarchy |
| Review state machine and concurrency | Tested | Mandatory expected revision, atomic stale-review rejection, role caps |
| Stable canonical parcel identities | Tested | Accepted source membership; separate baseline approval; stable UUID across publications |
| Boundary rejection/defer policy | Tested | Reject retains baseline; pending/deferred selected changes block |
| Resulting candidate/neighborhood validation | Tested | Exact fingerprint and current policy; overlap checks on assembled canonical geometries |
| Immutable versions, lineage, exclusions | Tested | Included sources/hashes/mappings/CRS/matches/reviews/selections; unselected reasons |
| Dated identity-linked vector changes | Tested core | Explicit chronological pair, metric tolerance; duplicates/splits abstain; missing does not imply demolition |
| Durable jobs | Tested | Rollback of stage side effects, retry and duplicate delivery; actual Redis/Celery worker |
| Job telemetry | Partial | Durable receipts/stages, no fake progress; running state may remain externally queued until transaction commits |
| GeoJSON/GeoPackage/CSV/quality/lineage exports | Tested core | Reopened geometry/CRS/JSON attributes; CSV one row per source link |
| Traceable rollback | Tested | New version, old snapshot retained |
| Migrations and restart persistence | Tested | Original-schema additive upgrade; browser retrieves publication after API restart |
| PostgreSQL/PostGIS native geometry/GiST | Tested | Native PostgreSQL 18.6 / PostGIS 3.6.2 integration |
| Full Compose deployment | Tested | Fresh-volume startup; Chromium/Nginx upload-through-publication; real Celery job; all five exports; API restart; separate-stack database/upload restore verified through browser/API |
| Secure defaults and opt-in seed | Implemented | Required JWT secret outside demo, explicit seeding, pinned Python/Node dependencies |
| Synthetic evaluation pack | Generated/measured | ~1,000 parcels, separate answer key, checksums/vertices/dependencies/results in docs |
| Accuracy/effort objectives | Targets, not generally achieved claims | Scoped synthetic metrics only; schema-suggestion accuracy/manual savings not measured |

## Actual verification

- **23 backend tests passed** against real PostGIS and Redis/Celery.
- **1 Chromium E2E passed** against the real PostGIS API, including download and restart.
- Compose Chromium publication/export/restart and separate-stack restore probes passed.
- Frontend typecheck/build passed; MapLibre bundle warning remains.
- Latest synthetic evaluation: **14.0621 seconds**, including simulated review.
- Detailed commands, environment versions, denominators and limitations:
  [`docs/VERIFICATION.md`](docs/VERIFICATION.md).

## Roadmap / unavailable

Trained GNN, probability calibration, imagery change detection, CAD/scans/point
clouds/RINEX, ground-control legacy-map transformation, installable offline PWA,
government/Aadhaar/DigiLocker/NAPIX integrations, blockchain, AR, advanced 3D,
citizen portal, natural-language spatial queries and Kubernetes are not implemented.
No legal compliance, government approval or real-world accuracy claim is made.
