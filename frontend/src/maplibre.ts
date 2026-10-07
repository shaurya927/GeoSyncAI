import { setWorkerUrl } from 'maplibre-gl'
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url'

// Bundle the patched MapLibre worker with Vite in development and production.
// A raw asset URL would omit the worker's shared-module imports in production.
setWorkerUrl(workerUrl)

export * from 'maplibre-gl'
