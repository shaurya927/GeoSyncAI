# GeoSyncAI SIH submission checklist

**Problem:** SIH26013 — Automated Integration and Intelligent Harmonization of
Multi-source Geospatial Data for Urban Land Record Management

**Team:** Git Good · **Team ID:** 151551

## Presentation-ready feature/claim table

| Feature | Honest claim | Evidence |
|---|---|---|
| Multi-source ingestion | GeoJSON, CSV, SHP ZIP, GeoPackage and immutable hashes/raw bytes | Backend regression and pinned container suite |
| CRS/schema review | Unknown CRS gates spatial processing; confirmed bilingual/template mappings are versioned | Mapping/CRS tests and Dataset UI |
| Explainable matching | Deterministic spatial/semantic candidates with alternatives, abstention and uncertainty | Synthetic evaluation and review UI |
| Learned ranking | Reviewed group-aware ranker can be activated for inference only after held-out validation; scores remain uncalibrated | Ranker activation test |
| Reconciliation | Three independent sources can be accepted into explicit membership and selections; competing values remain visible | Production three-source decision/per-field selection browser path |
| Boundary editing | Move/split/merge/shared-edge drafts, undo/cancel and server approval gates | Production drawn-cut/drag/undo/split/merge browser test and neighbor/stale topology regressions |
| Fieldwork PWA | Bounded assignment-scoped offline evidence with idempotency and conflict resubmission | Production offline reload/reconnect/revocation/account/expiry/quota browser regression |
| Citizen access | Explicit, revocable grants return frozen published fields and version provenance | Frozen grant regression |
| Compliance | Versioned typed screening rules with insufficient-information and not-applicable results | Compliance API/UI |
| Raster/3D | Genuine GeoTIFF metadata/preview and validated-height-only extrusion | rasterio container test; no surveyed volumetric twin claim |
| Publication | Immutable candidate fingerprint, manifest/output/source hashes, lineage, exports and rollback verification | Current backend suite plus publication/browser path; service boundary in final acceptance |
| Interoperability | Scoped OGC landing, conformance, collections, items, bbox, version and pagination | OGC backend tests |
| Security and traffic | Revocable scoped sessions, stronger passwords, account administration, shared rate limits, request/queue caps, CSP, trusted proxy and non-root containers | Security API/browser regressions, dependency audit and service-stack CI; hosting boundaries in SECURITY.md |

## Demo path

1. Copy `.env.example` to `.env`, fill random `JWT_SECRET` and `POSTGRES_PASSWORD`.
2. Run `docker compose up --build -d`, then `docker compose run --rm api python -m app.seed_demo`.
3. Open `http://localhost:5173`, sign in with an explicitly seeded isolated demo account.
4. Create a project, upload two synthetic one-parcel files and an attribute-only revenue CSV.
5. Confirm CRS, accept mapping suggestions, run matching, review identity separately from boundary/attributes,
   create an explicit reconciliation, and inspect competing evidence.
6. Use Boundary editor for a draft, review it, validate the exact candidate, publish, export GeoJSON/CSV/lineage,
   and use Versions & history to verify or create a traceable rollback.
7. Use Fieldwork for assignment-scoped evidence and the citizen account only after a reviewer creates a frozen grant.
8. Use Read-only queries, Compliance screening, Ground-control, and the genuine GeoTIFF registry as extended examples.

## Required submission disclosures

- Synthetic metrics are not real cadastral accuracy or manual-effort savings.
- Compliance output is screening assistance, not a legal certificate.
- Citizen access is explicit grant-based; no Aadhaar, DigiLocker, NAPIX or government identity integration is claimed.
- Raster preview is a bounded source preview; no photogrammetric reconstruction or surveyed 3D digital twin is claimed.
- Uncalibrated ranking scores are not probabilities.
- Same-database administrators can replace trusted data and hashes; integrity verification detects ordinary artifact/source changes within its trust boundary.
- Fresh acceptance evidence and service verification boundaries are recorded in `docs/FINAL_ACCEPTANCE.md` and `WORK_PROGRESS.md`.
