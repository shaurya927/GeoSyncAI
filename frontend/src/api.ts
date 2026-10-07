export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000/api').replace(/\/$/, '')
export type ExportKind = 'geojson' | 'gpkg' | 'csv' | 'lineage' | 'quality'

export class ApiError extends Error {
  constructor(message: string, readonly status = 0) { super(message); this.name = 'ApiError' }
}

async function responseError(response: Response) {
  let message = `API returned HTTP ${response.status}`
  try {
    const body = await response.json() as { detail?: unknown }
    if (body.detail) message = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
  } catch { /* retain HTTP status for non-JSON errors */ }
  return new ApiError(message, response.status)
}

function authorizedHeaders(options: RequestInit): Headers {
  const headers = new Headers(options.headers)
  const token = localStorage.getItem('geosyncai_token')
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  return headers
}

export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  let response: Response
  try { response = await fetch(`${API_BASE_URL}${path}`, { ...options, headers: authorizedHeaders(options) }) }
  catch { throw new ApiError(`Cannot reach API at ${API_BASE_URL}. Check the connection and retry.`) }
  if (!response.ok) throw await responseError(response)
  if (response.status === 204) return undefined as T
  return await response.json() as T
}

export async function downloadExport(projectId: string, kind: ExportKind, versionId?: string, outputCrs = 'EPSG:4326') {
  const crs = encodeURIComponent(outputCrs)
  const path = versionId ? `/projects/${projectId}/versions/${versionId}/export?format=${kind}&output_crs=${crs}` : `/projects/${projectId}/exports/${kind}?output_crs=${crs}`
  const response = await fetch(`${API_BASE_URL}${path}`, { headers: authorizedHeaders({}) })
  if (!response.ok) throw await responseError(response)
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `geosyncai-${versionId || 'latest'}.${kind === 'lineage' || kind === 'quality' ? `${kind}.json` : kind}`
  anchor.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export async function rasterPreview(projectId: string, assetId: string): Promise<string> {
  const response = await fetch(`${API_BASE_URL}/projects/${projectId}/raster-assets/${assetId}/preview`, { headers: authorizedHeaders({}) })
  if (!response.ok) throw await responseError(response)
  return URL.createObjectURL(await response.blob())
}
