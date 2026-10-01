# GeoSyncAI requirement status

Status reflects this workspace, not a claim about the official SIH submission
or real-world cadastral accuracy.

## Required demonstration functionality

| Requirement | Status | Evidence / boundary |
|---|---|---|
| Browser-based responsive officer workspace | Implemented | React + TypeScript + Vite, responsive CSS, and MapLibre map |
| Authentication | Implemented | JWT bearer tokens with viewer/processor/reviewer/admin accounts |
| Project-scoped permissions | Implemented | Backend membership checks on reads, writes, reviews, queries, and exports |
| Multi-source ingestion | Implemented | GeoJSON, CSV, GeoPackage, and safe SHP ZIP ingestion paths |
| Preserve original source records | Implemented | Immutable raw upload path and SHA-256 hash; normalized features are separate |
| Format and archive validation | Implemented | Parser checks, required SHP components, safe ZIP paths, readable GeoPackage layers |
| Schema and record validation | Implemented | Schema field report, duplicate IDs, empty/invalid geometry, processed/quarantined counts |
| CRS validation and normalization | Implemented | Declared/embedded CRS parsing and PROJ transformation; unresolved CRS is explicit |
| Explicit unmatched outcome | Implemented | Match proposals may remain `unmatched`; no forced nearest-neighbor link |
| Explainable candidate matching | Implemented | Identifier agreement, IoU, centroid distance, area difference, alternatives |
| Separate score and review dimensions | Implemented | Uncalibrated rule score, spatial evidence, data quality, and review status are distinct |
| Ambiguity and abstention | Implemented | Configurable ambiguity margin routes close candidates to review |
| Attribute-only revenue records | Implemented | CSV rows without geometry remain processed; spatial evidence is explicitly unavailable |
| Attribute mapping | Partial | Curated aliases and raw fields exist; reusable reviewed mapping-template editor is not included |
| Topology/conflict detection | Implemented | Invalid geometry and overlaps; dated attribute/geometry change records |
| Reversible change proposals | Implemented | Before/after geometry and attributes, area delta, tolerance, and evidence retained |
| Authorized boundary approval | Implemented | Reviewer role required; boundary changes block publication until accepted and validated |
| Officer review decisions | Implemented | Accept/reject/defer and rationale persisted as review records |
| Validation before publication | Implemented | Accepted change geometry and open topology errors are checked |
| Versioned publication | Implemented | Immutable numbered versions with lineage manifests |
| Full publication lineage | Implemented | Dataset/hash, source feature, accepted changes, and review decision IDs retained |
| GeoJSON/CSV/lineage export | Implemented | Latest-version browser exports and version-specific export route |
| Dated vector change detection | Implemented | Added/missing/attribute/geometry changes with tolerance evidence |
| Background jobs | Implemented | Durable job records, restart recovery, local background fallback, optional Celery seam |
| PostgreSQL/PostGIS persistence | Implemented | Compose PostGIS image and native geometry columns/GiST when configured |
| Reproducible synthetic data | Implemented | Deterministic labelled parcel pair with non-legal-evidence warning |
| Honest evaluation reporting | Implemented as safeguard | No fabricated benchmark or accuracy result is shown |

## Roadmap or explicitly out of scope

| Item | Status |
|---|---|
| CAD, scans, point clouds, drone/RINEX processing | Not implemented; separate integrations |
| Ground-control-point legacy-map transformation | Not implemented |
| Learned ranker, embeddings, calibration, reliability plots | Not claimed; MVP uses deterministic rules |
| GNN/graph matcher | Not implemented |
| Imagery change detection | Not implemented |
| Offline field synchronization / installable PWA | Not implemented; responsive browser access is delivered |
| Production OGC API Features conformance | Not claimed; exports are implemented |
| Government API integrations | Not implemented; require authorized access |
| Kubernetes/GPU/distributed production scaling | Not implemented; Compose prototype only |
| Blockchain, Aadhaar, citizen portal, predictive values, chatbot, AR, 3D | Explicitly outside MVP |

## Known external dependencies and limitations

- PostgreSQL/PostGIS Compose configuration is provided; local automated tests use
  isolated SQLite, so the target deployment should run a PostGIS smoke test.
- Fiona/GDAL native wheels may need platform-specific installation support for
  SHP ZIP and GeoPackage uploads.
- Demonstration map tiles use the public OpenStreetMap raster endpoint; use an
  approved provider and attribution for deployment.
- Synthetic data demonstrates pipeline behavior only. It does not establish
  accuracy, calibration, legal validity, ownership, or official record status.
