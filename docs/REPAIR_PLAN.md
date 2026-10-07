# Submission repair plan — 7 October 2026

User authorization: implement the remaining errors, test/correct them, and return the final project. Work locally on `codex/submission-fixes-20261007`, based on `10eb59a`. Preserve existing databases, uploads and user volumes. No remote push or public deployment is part of this task.

Subsequent authorization: publish the completed repairs to the existing public
`shaurya927/GeoSyncAI` repository using the user's collaborator account. This
extends the original delivery scope to a Git push, without public deployment.

1. Close query authorization/classification bypasses; implement actual ward/date/bbox/radius filtering and pagination.
2. Give ground-control residuals explicit metre units, validate coordinate/CRS consistency and enforce independently validated control coverage before application. Preserve original source metadata and create a linked normalized version.
3. Restore account/project-scoped offline fieldwork after reload, with expiry, storage failure handling, logout/account switching and server reauthorization on reconnect.
4. Replace envelopes with true split/intersection/union; provide map cut drawing, vertex dragging/snapping/shared-edge edits and meaningful previews. Connect reconciliation decisions/source selection in the web UI.
5. Add backend and production browser regressions for the independent failures and connected journeys. Correct failures, synchronize documentation, and return clean source ZIP plus local launch instructions.

Known environment: Windows, Python 3.12 review runtime, Node, Chromium and geospatial dependencies available. Docker and local PostGIS/Redis were unavailable at planning time. Local results and any external/previous service evidence must remain distinct.

Completion requires functioning authorized workflows and fresh evidence. Optional government integrations, photogrammetry and GNN research remain explicitly outside the submission prototype.
