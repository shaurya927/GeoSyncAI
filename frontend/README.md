# Officer workspace frontend

React 19 + TypeScript + Vite + MapLibre. The existing forest/mint responsive visual
system is retained in `src/styles.css`; the workspace now uses only live API state.

```bash
npm ci
npm run dev
npm run typecheck
npm run build
```

`VITE_API_BASE_URL` defaults to `http://localhost:8000/api`. The Compose build uses
`/api` through Nginx. Synthetic generation is an explicit backend action, not a
frontend mock mode. No proprietary map token is required; vectors render on a
local background by default. OpenStreetMap tiles are optional.

## Views

- Overview: real current user, project creation/selection, live counts, explicit
  synthetic setup, versioned policy and administrator membership controls.
- Datasets: source upload/date/organization, original hash, quality/record preview,
  CRS correction, mapping confirmation/reuse, pair selection and processing jobs.
- Review: exact feature UUID selection, full polygon/multipolygon map geometry,
  evidence/alternatives, rationale and stale-review handling, canonical baseline
  and per-field attribute source selection.
- Change alerts: dated vector proposals, before/after overlays and review.
- Versions: validation failures/exclusions, publication, lineage inspection,
  version-specific lineage download, GeoPackage CRS and traceable restore.

`src/workflowApi.ts` defines live response shapes. `src/api.ts` handles bearer
authentication, errors and downloads. Authorization is enforced by the backend;
frontend disabled states are only usability hints. Original mock data and fallback
square geometry have been removed.

The real browser acceptance test is `e2e/test_browser.py`, run from the repository
root with the Python development dependencies and Playwright Chromium installed.
It uses actual API uploads, CRS/mapping confirmation, review, publication/download
and API restart, and checks a narrow-screen layout.
