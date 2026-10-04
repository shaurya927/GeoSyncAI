# Architecture and invariants

## Layers

- `frontend/src/App.tsx`: existing React/Vite officer workspace and MapLibre map.
- `backend/app/main.py`: scoped FastAPI transport, auth and workflow commands.
- `services.py`: ingestion, confirmed semantics, indexed matching/topology and
  dated comparison. `spatial.py` enforces coordinate and CRS transformation rules.
- `publication.py`: reviewed candidate assembly, exact fingerprint, neighborhood
  validation and immutable feature snapshots. `policy.py` loads append-only policy
  revisions from audited commands.
- `tasks.py`: transactional durable stages; local executor or Redis/Celery.
- SQLAlchemy persistence with native PostGIS geometry/GiST when using PostgreSQL;
  GeoJSON-compatible JSON remains available in both storage profiles.

## Data model

`Project` scopes membership, datasets, decisions, jobs, policies and versions.
`Dataset` represents an immutable source upload/version; `parent_dataset_id`
links replacement snapshots. Source organization, capture/upload dates, original
filename, hash, classification, accuracy metadata, source/display/analysis CRS
and transformation information are stored independently.

`SourceFeature` holds original attributes/geometry and separately normalized
geometry, confirmed canonical attributes, administrative context and processing
state/reason. All raw upload bytes remain available under UUID-named files and
their SHA-256 hashes; storage is **hash-verified, not hash-addressed**.

`SchemaMapping` versions retain officer confirmation and source field summaries.
`MatchProposal` retains candidate scores/evidence; `ChangeProposal` retains
before/after values, metric displacement, tolerance and affected source neighbors.
`ReviewDecision` retains actor, rationale, time and expected/resulting revision.

Accepted identity decisions create stable `ParcelEntity` UUIDs and explicit
`ParcelSourceLink` membership. `ParcelSelection` is a **separate** reviewer command:
geometry source, default attribute source, optional per-field overrides and
revision. This separates identity, geometry, attributes and approval status.

`PublishedVersion` owns immutable `PublicationFeature` snapshots, source links,
mapping/CRS/matching/review lineage, the exact validation report and exclusions.
Restoring a historical version copies its snapshot into a new numbered version.
`AuditEvent` retains mutations and policy/validation history. `Job` retains input/
configuration hashes, attempts, result and errors; each receipt represents a
single transactionally committed processing stage.

## Publication policy

- Only explicitly selected, reviewer-approved baselines are included.
- Unselected/quarantined records are listed with exclusion reasons.
- Unknown/implausible CRS is a project-wide spatial publication blocker.
- Confirmed schema mappings are required for included source links.
- Accepting identity never chooses/merges a boundary or an attribute value.
- Pending/deferred changes touching included parcels block publication.
- Rejection does not apply the rejected edit; the selected baseline is retained.
- Accepted edits are assembled into the candidate, then validated together with
  the other selected canonical parcels. Resulting overlaps (including containment
  and duplicates) block according to the versioned area tolerance.
- A change to input metadata, policy, membership, selection or decisions invalidates
  prior validation through project revision and/or exact candidate fingerprint.
- Source metadata already used in reviewed links must be changed by uploading a
  new dataset version. Unreviewed evidence is explicitly superseded after correction.

## Spatial behavior and limits

Missing CRS never implies EPSG:4326. Every coordinate (including rings/holes and
multipart components) is checked for finite values; geographic coordinates must
be plausible. PROJ transforms use `always_xy=True`, error checking, no ballpark
fallback and the best available operation. Transformation metadata records the
operation, software versions, operation accuracy (when known) and grid availability.
No transformation is described as proof of boundary accuracy.

Display/GeoJSON use EPSG:4326. Metric work uses a local UTM projection derived
from source location; polar/wide-area/antimeridian projects require a broader
analysis-CRS policy before production use. An STRtree restricts metric candidates
and topology pair checks. Identity semantics and common administrative context
must be compatible; a shared number cannot override distant geometry. Attribute-only
matching requires mapped namespace evidence. Split/merge and competing assignments
abstain rather than forcing a one-to-one relationship.

Potential coverage gaps are evaluated only when the project has an explicit
coverage polygon. Valid source holes are preserved. General shared-boundary
reconstruction and approved split/merge editing remain limitations; no automatic
repair or cadastral correctness is claimed.

## Technical references used

- [pyproj Transformer and TransformerGroup](https://pyproj4.github.io/pyproj/stable/api/transformer.html):
  axis order, error checking, operation/grid availability and transformation accuracy.
- [Shapely STRtree](https://shapely.readthedocs.io/en/stable/strtree.html):
  returned geometry indices, bounding-box queries, `dwithin` and predicate behavior.

## Security / deployment boundary

PBKDF2 password hashes, bearer JWT, global role caps plus project membership,
server-checked reviews/publication and authenticated downloads. Demonstration
accounts are opt-in. Upload/archive limits are configurable. Raw records are not
sent to external AI/model services. Optional basemap requests are browser tile
requests, not uploads of source records.

This is still a prototype: do not equate its audit log with legal certification,
or its synthetic evaluation with operational cadastral accuracy.
