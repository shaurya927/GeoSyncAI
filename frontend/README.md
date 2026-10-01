# GeoSyncAI officer workspace

React + TypeScript + Vite frontend for the GeoSyncAI land-data harmonization workflow. The interface uses MapLibre GL JS for parcel visualization and integrates with a FastAPI service by default.

## Run locally

Requires Node.js 20.19+ (or 22.12+).

```bash
cd frontend
cp .env.example .env
npm install
npm run dev
```

Open <http://localhost:5173>. Set `VITE_API_BASE_URL` in `.env` if FastAPI is not served from `http://localhost:8000/api`.

The login screen also exposes **Use local synthetic demo** when `VITE_ENABLE_DEMO_MOCK=true`. This mode is deliberately isolated in `src/mockApi.ts`, is visibly marked in the UI, and never implies a backend connection or real cadastral accuracy. Set the flag to `false` for API-only deployments.

## Checks

```bash
npm run typecheck
npm run build
npm run preview
```

## FastAPI contract

The client uses bearer-token authentication and these JSON endpoints. List endpoints may return either a JSON array or an object wrapping that array under the shown plural key.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/auth/token` | Body `{ username, password }`; returns `{ access_token, user }` |
| `GET` | `/projects` | List scoped projects |
| `POST` | `/projects` | Create a project when none is available |
| `GET` | `/projects/{id}/datasets` | Dataset registry and validation/CRS status |
| `POST` | `/projects/{id}/datasets/upload` | Multipart upload under field `file` |
| `POST` | `/projects/{id}/bootstrap-synthetic` | Create deterministic synthetic parcel datasets |
| `POST` | `/projects/{id}/match` | Run explainable baseline matching |
| `GET` | `/projects/{id}/matches` | Match proposals and evidence cards |
| `POST` | `/projects/{id}/reviews/match/{proposal_id}` | Save reviewer decision and rationale |
| `GET` | `/projects/{id}/changes` | Dated-snapshot change alerts |
| `GET` | `/projects/{id}/versions` | Publication/version history |
| `POST` | `/projects/{id}/validate` | Run publication checks |
| `POST` | `/projects/{id}/publish` | Publish a reviewed version |
| `GET` | `/projects/{id}/datasets/{dataset_id}/features` | Normalized source features for MapLibre |
| `GET` | `/projects/{id}/exports/{geojson\|csv\|lineage}` | Export latest published output |

The TypeScript response shapes are documented in `src/types.ts`. FastAPI must allow the Vite development origin in CORS. Authentication and project/download authorization remain backend responsibilities.

## Interface safeguards

- Ranking scores are explicitly labeled **uncalibrated** and never described as probabilities.
- Boundary-changing proposals require a visible before/after acknowledgement.
- Missing CRS blocks spatial matching rather than silently assuming a system.
- Accept/reject/defer decisions and review notes are presented as audit events.
- Synthetic mode and synthetic exports are clearly labeled.
