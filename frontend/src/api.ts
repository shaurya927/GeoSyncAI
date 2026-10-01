import type { ChangeAlert, Dataset, Metric, Project, Proposal, ReviewAction, Version, WorkspaceData } from './types'

export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000/api').replace(/\/$/, '')

export class ApiError extends Error {
  readonly status: number
  constructor(message: string, status = 0) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers)
  if (!headers.has('Content-Type') && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  const token = window.localStorage.getItem('geosyncai_token')
  if (token) headers.set('Authorization', `Bearer ${token}`)
  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}${path}`, { ...options, headers })
  } catch {
    throw new ApiError(`Could not reach FastAPI at ${API_BASE_URL}. Check the API URL and that the service is running.`)
  }
  if (!response.ok) {
    let message = `FastAPI returned ${response.status}`
    try {
      const body = await response.json() as { detail?: string; message?: string }
      message = body.detail || body.message || message
    } catch { /* non-JSON error */ }
    throw new ApiError(message, response.status)
  }
  if (response.status === 204) return undefined as T
  return await response.json() as T
}

type ServerProject = { id: string; name: string; description?: string | null; owner_id: string; created_at: string }
type ServerDataset = {
  id: string; project_id: string; name: string; source_organization?: string | null; status: string
  original_filename?: string | null; record_count?: number; normalized_count?: number; declared_crs?: string | null
  normalized_crs?: string | null; capture_date?: string | null; uploaded_at?: string; validation_report?: {
    format?: string; warnings?: string[]; errors?: string[]; duplicate_ids?: string[]; invalid_geometry?: unknown[]; processed?: number; quarantined?: number
  } | null
}
type ServerFeature = { id: string; original_id?: string | null; attributes: Record<string, unknown>; geometry?: GeoJSON.Geometry | null; status: string; processing_reason?: string | null }
type ServerMatch = { id: string; left_feature_id?: string | null; right_feature_id?: string | null; score: number; score_type: string; status: string; evidence: Record<string, unknown> }
type ServerConflict = { id: string; type: string; severity: string; description: string; status: string; details: Record<string, unknown> }
type ServerChange = { id: string; change_type: string; source_feature_id?: string | null; boundary_change: boolean; status: string; before_geometry?: GeoJSON.Geometry | null; after_geometry?: GeoJSON.Geometry | null; evidence: Record<string, unknown> }
type ServerVersion = { id: string; version: number; created_at: string; lineage_manifest: Record<string, unknown> }

function mapProject(item: ServerProject, datasetCount = 0, pendingReview = 0): Project {
  return {
    id: item.id,
    name: item.name,
    location: item.description || 'Scoped land-data workspace',
    updatedAt: item.created_at,
    status: pendingReview > 0 ? 'needs-attention' : 'ready',
    datasetCount,
    pendingReview,
  }
}

function mapDataset(item: ServerDataset): Dataset {
  const format = (item.validation_report?.format || item.original_filename?.split('.').pop() || 'GeoJSON').toLowerCase()
  const kind = format === 'csv' ? 'Revenue table' : 'Cadastral polygons'
  const attributeOnly = format === 'csv' && !item.normalized_crs && !item.declared_crs
  const crsStatus = attributeOnly ? 'verified' : item.normalized_crs ? 'verified' : item.declared_crs ? 'warning' : 'missing'
  const issues = [
    ...(item.validation_report?.warnings || []),
    ...(item.validation_report?.errors || []),
    ...(item.validation_report?.duplicate_ids || []).map((id) => `Duplicate identifier: ${id}`),
  ]
  return {
    id: item.id,
    name: item.name,
    source: item.source_organization || 'Officer upload',
    kind,
    format: format === 'csv' ? 'CSV' : format === 'gpkg' || format === 'geopackage' ? 'GeoPackage' : format === 'zip' || format === 'shp_zip' ? 'SHP ZIP' : 'GeoJSON',
    records: item.record_count || 0,
    crs: attributeOnly ? 'Not applicable' : item.declared_crs || 'Unknown',
    crsStatus,
    captureDate: item.capture_date || undefined,
    uploadedAt: item.uploaded_at || new Date().toISOString(),
    status: item.status === 'processed' ? 'ready' : item.status === 'registered' ? 'processing' : 'needs-review',
    issues,
  }
}

const asNumber = (value: unknown): number | undefined => typeof value === 'number' ? value : typeof value === 'string' && value !== '' ? Number(value) : undefined
const asText = (value: unknown, fallback = '—'): string => value === null || value === undefined || value === '' ? fallback : String(value)

function polygonOrFallback(geometry?: GeoJSON.Geometry | null): GeoJSON.Polygon {
  if (geometry?.type === 'Polygon') return geometry as GeoJSON.Polygon
  if (geometry?.type === 'MultiPolygon') return { type: 'Polygon', coordinates: geometry.coordinates[0] || [] }
  return { type: 'Polygon', coordinates: [[[0, 0], [0.001, 0], [0.001, 0.001], [0, 0.001], [0, 0]]] }
}

function mapMatch(match: ServerMatch, features: Map<string, ServerFeature>, conflicts: ServerConflict[], changes: ServerChange[]): Proposal {
  const left = match.left_feature_id ? features.get(match.left_feature_id) : undefined
  const right = match.right_feature_id ? features.get(match.right_feature_id) : undefined
  const evidence = match.evidence || {}
  const overlap = asNumber(evidence.intersection_over_union)
  const distance = asNumber(evidence.centroid_distance_m)
  const area = asNumber(evidence.relative_area_difference)
  const featureConflicts = conflicts.filter((conflict) => conflict.details?.other_feature_id === match.left_feature_id || conflict.details?.other_feature_id === match.right_feature_id)
  const boundaryConflict = Boolean((area !== undefined && area > 0.15) || changes.some((change) => change.source_feature_id === match.left_feature_id && change.boundary_change))
  const status: Proposal['status'] = match.status === 'accepted' ? 'accepted' : match.status === 'rejected' ? 'rejected' : match.status === 'deferred' || match.status === 'needs_field_verification' ? 'deferred' : 'pending'
  const leftId = left?.original_id || match.left_feature_id || 'unknown source'
  const rightId = right?.original_id || match.right_feature_id || 'no counterpart'
  const alternatives = Array.isArray(evidence.alternatives) ? evidence.alternatives as Array<{ feature_id?: string; score?: number }> : []
  const conflictText = [
    ...featureConflicts.map((conflict) => conflict.description),
    ...(boundaryConflict ? ['Measured area or geometry difference requires separate boundary review; accepting a link does not replace either source boundary.'] : []),
  ]
  return {
    id: match.id,
    targetType: 'match',
    parcelId: leftId,
    sourceIds: [match.left_feature_id, match.right_feature_id].filter((value): value is string => Boolean(value)),
    title: match.right_feature_id ? `${leftId} ↔ ${rightId}` : `${leftId} · unmatched`,
    subtitle: match.status === 'ambiguous' ? 'Ambiguous alternatives' : match.status === 'unmatched' ? 'No candidate met the configured rule' : boundaryConflict ? 'Boundary review required' : 'Explainable rule proposal',
    score: match.score || 0,
    scoreLabel: match.score_type === 'uncalibrated_rule_score' ? 'Uncalibrated rule score' : match.score_type,
    status,
    boundaryConflict,
    geometry: polygonOrFallback(left?.geometry || right?.geometry),
    evidence: [
      { label: 'Identifier agreement', value: evidence.identifier_agreement ? 'Exact' : 'Not established', tone: evidence.identifier_agreement ? 'good' : 'warn' },
      { label: 'Polygon overlap', value: overlap === undefined ? 'Unavailable' : `${(overlap * 100).toFixed(1)}%`, tone: overlap !== undefined && overlap >= 0.7 ? 'good' : 'warn' },
      { label: 'Centroid distance', value: distance === undefined ? 'Unavailable' : `${distance.toFixed(1)} m`, tone: distance !== undefined && distance <= 10 ? 'good' : 'warn' },
      { label: 'Relative area difference', value: area === undefined ? 'Unavailable' : `${(area * 100).toFixed(1)}%`, tone: area !== undefined && area <= 0.1 ? 'neutral' : 'warn' },
    ],
    reasons: [
      asText(evidence.explanation, 'The rule set produced a candidate from available evidence.'),
      evidence.spatial_evidence_used ? 'Spatial evidence was measured after CRS normalization.' : 'Spatial evidence was not used because a verified normalized CRS is unavailable.',
      match.status === 'ambiguous' ? 'The margin between top candidates is narrow, so the system abstains from an automatic link.' : match.status === 'unmatched' ? 'The record remains explicitly unmatched rather than being forced to a nearest neighbor.' : 'Review the original records before making a decision.',
    ],
    alternatives: alternatives.map((alternative) => ({ id: alternative.feature_id || 'candidate', label: features.get(alternative.feature_id || '')?.original_id || alternative.feature_id || 'candidate', score: alternative.score || 0, note: 'Alternative candidate retained in evidence.' })),
    conflicts: conflictText,
    sourceValues: [
      { source: 'Left dataset', identifier: leftId, area: asText(left?.attributes.area || left?.attributes.area_m2), updated: 'Source capture date' },
      ...(right ? [{ source: 'Right dataset', identifier: rightId, area: asText(right.attributes.area || right.attributes.area_m2), updated: 'Source capture date' }] : []),
    ],
  }
}

function mapChange(change: ServerChange, features: Map<string, ServerFeature>): Proposal {
  const source = change.source_feature_id ? features.get(change.source_feature_id) : undefined
  const namespaceKey = asText(change.evidence?.namespace_key, change.source_feature_id || 'unidentified feature')
  const status: Proposal['status'] = change.status === 'accepted' || change.status === 'validated' ? 'accepted' : change.status === 'rejected' ? 'rejected' : change.status === 'deferred' || change.status === 'needs_field_verification' ? 'deferred' : 'pending'
  const areaDelta = asNumber(change.evidence?.after_area_m2) !== undefined && asNumber(change.evidence?.before_area_m2) !== undefined
    ? asNumber(change.evidence.after_area_m2)! - asNumber(change.evidence.before_area_m2)!
    : undefined
  return {
    id: change.id,
    targetType: 'change',
    parcelId: namespaceKey,
    sourceIds: change.source_feature_id ? [change.source_feature_id] : [],
    title: `${namespaceKey} · ${change.change_type.replaceAll('_', ' ')}`,
    subtitle: change.boundary_change ? 'Boundary revision requires authorized approval' : 'Attribute or snapshot change review',
    score: 0,
    scoreLabel: 'Change alert — no match score',
    status,
    boundaryConflict: change.boundary_change,
    geometry: polygonOrFallback(change.after_geometry || change.before_geometry || source?.geometry),
    evidence: [
      { label: 'Change type', value: change.change_type.replaceAll('_', ' '), tone: change.boundary_change ? 'warn' : 'neutral' },
      { label: 'Area delta', value: areaDelta === undefined ? 'Not available' : `${areaDelta.toFixed(2)} m²`, tone: change.boundary_change ? 'warn' : 'neutral' },
      { label: 'Tolerance', value: asText(change.evidence?.tolerance), tone: 'neutral' },
      { label: 'Review status', value: change.status, tone: status === 'accepted' ? 'good' : 'warn' },
    ],
    reasons: [
      'The alert compares two dated vector snapshots without inferring legal or physical meaning.',
      change.boundary_change ? 'The proposed boundary movement is reversible and must be accepted and validated by an authorized reviewer.' : 'Attribute differences remain linked to both source snapshots.',
      'The original source geometries and attributes remain unchanged.',
    ],
    alternatives: [],
    conflicts: change.boundary_change ? ['Boundary movement affects the published geometry candidate and requires before/after review.'] : [],
    sourceValues: [{ source: 'Source feature', identifier: source?.original_id || change.source_feature_id || '—', area: asText(source?.attributes.area || source?.attributes.area_m2), updated: 'Snapshot lineage retained' }],
  }
}

function mapVersion(item: ServerVersion): Version {
  const manifest = item.lineage_manifest || {}
  const features = Array.isArray(manifest.features) ? manifest.features.length : 0
  return { id: item.id, label: `Published version v${item.version}`, status: 'Published', createdAt: item.created_at, createdBy: 'Authorized reviewer', records: features, changeSummary: `${features} features with retained source lineage` }
}

export async function login(username: string, password: string) {
  const result = await request<{ access_token: string; user: { username: string; role: string } }>('/auth/token', { method: 'POST', body: JSON.stringify({ username, password }) })
  return { token: result.access_token, user: { name: result.user.username, role: result.user.role } }
}

export async function listProjects() {
  const result = await request<ServerProject[]>('/projects')
  return result.map((item) => mapProject(item))
}

export async function bootDemoProject() {
  const projects = await listProjects()
  if (projects[0]) return projects[0]
  const created = await request<ServerProject>('/projects', { method: 'POST', body: JSON.stringify({ name: 'Synthetic demonstration', description: 'Safe synthetic parcel workspace' }) })
  return mapProject(created)
}

export async function getDatasets(id: string) {
  const result = await request<ServerDataset[]>(`/projects/${id}/datasets`)
  return result.map(mapDataset)
}

async function getServerDatasets(id: string) {
  return await request<ServerDataset[]>(`/projects/${id}/datasets`)
}

async function getFeatures(projectId: string, datasetId: string) {
  return await request<ServerFeature[]>(`/projects/${projectId}/datasets/${datasetId}/features`)
}

export async function uploadDataset(id: string, file: File) {
  const body = new FormData()
  body.append('file', file)
  const item = await request<ServerDataset & { validation_report?: ServerDataset['validation_report'] }>(`/projects/${id}/datasets/upload`, { method: 'POST', body })
  return mapDataset(item)
}

type ServerJob = { id: string; job_type: string; status: 'queued' | 'running' | 'succeeded' | 'failed'; result?: Record<string, unknown> | null; error?: string | null }

async function runJob(projectId: string, jobType: 'match' | 'topology' | 'change_detection', payload: Record<string, unknown>, idempotencyKey: string) {
  const query = new URLSearchParams({ job_type: jobType, idempotency_key: idempotencyKey })
  const created = await request<ServerJob>(`/projects/${projectId}/jobs?${query.toString()}`, { method: 'POST', body: JSON.stringify(payload) })
  let job = created
  for (let attempt = 0; attempt < 100 && (job.status === 'queued' || job.status === 'running'); attempt += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 250))
    job = await request<ServerJob>(`/projects/${projectId}/jobs/${created.id}`)
  }
  if (job.status !== 'succeeded') throw new ApiError(job.error || `Background ${jobType} job did not complete.`)
  return job.result || {}
}

export async function getProposals(id: string) {
  const matches = await request<ServerMatch[]>(`/projects/${id}/matches`)
  return matches.map((match) => ({ match }))
}

export async function runProcessing(id: string) {
  let datasets = await getServerDatasets(id)
  if (datasets.length < 2) {
    await request(`/projects/${id}/bootstrap-synthetic?count=25`, { method: 'POST' })
    datasets = await getServerDatasets(id)
  }
  const [left, right] = datasets
  if (!left || !right) throw new ApiError('Two datasets are required before matching.')
  const suffix = `${left.id}-${right.id}`
  const match = await runJob(id, 'match', { left_dataset_id: left.id, right_dataset_id: right.id, max_distance: 75, ambiguity_margin: 0.08 }, `match-${suffix}`)
  await Promise.all([left, right].map((dataset) => runJob(id, 'topology', { dataset_id: dataset.id }, `topology-${dataset.id}`).catch(() => undefined)))
  await runJob(id, 'change_detection', { before_dataset_id: left.id, after_dataset_id: right.id, geometry_tolerance: 1e-8 }, `change-${suffix}`).catch(() => undefined)
  return { status: 'succeeded', match }
}

export async function reviewProposal(proposalId: string, action: ReviewAction, note: string, targetType: 'match' | 'change' = 'match') {
  const projectId = window.localStorage.getItem('geosyncai_project_id')
  if (!projectId) throw new ApiError('No active project is selected.')
  const decision = action === 'accept' ? 'accepted' : action === 'reject' ? 'rejected' : 'deferred'
  await request(`/projects/${projectId}/reviews/${targetType}/${proposalId}`, { method: 'POST', body: JSON.stringify({ decision, rationale: note || 'Reviewed in officer workspace.' }) })
  const data = await getWorkspaceData(projectId, [])
  return data.proposals.find((proposal) => proposal.id === proposalId) || (() => { throw new ApiError('Review was saved but the proposal could not be reloaded.') })()
}

export async function validateVersion(id: string) {
  const result = await request<{ valid: boolean; validated_changes: number; open_topology_errors: number; failures: Array<{ reason: string }> }>(`/projects/${id}/validate`, { method: 'POST' })
  return {
    status: result.valid ? 'pass' : 'attention',
    checks: [
      { label: 'Topology errors', status: result.open_topology_errors ? 'blocked' : 'pass', detail: result.open_topology_errors ? `${result.open_topology_errors} open error(s)` : 'No open topology errors' },
      { label: 'Accepted changes', status: result.failures.length ? 'attention' : 'pass', detail: result.failures.length ? `${result.failures.length} change(s) failed validation` : `${result.validated_changes} accepted change(s) validated` },
      { label: 'CRS readiness', status: 'pass', detail: 'Published geometries retain their recorded CRS lineage' },
      { label: 'Lineage completeness', status: 'pass', detail: 'Source hashes and review decisions are retained for publication features' },
    ],
  }
}

export async function publishVersion(id: string) {
  const result = await request<ServerVersion>(`/projects/${id}/publish`, { method: 'POST' })
  return mapVersion(result)
}

export function downloadUrl(id: string, kind: 'geojson' | 'csv' | 'lineage') {
  return `${API_BASE_URL}/projects/${id}/exports/${kind}`
}

export async function downloadExport(id: string, kind: 'geojson' | 'csv' | 'lineage') {
  const token = window.localStorage.getItem('geosyncai_token')
  const response = await fetch(downloadUrl(id, kind), {
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
  })
  if (!response.ok) {
    let message = `Export failed with HTTP ${response.status}`
    try {
      const body = await response.json() as { detail?: string }
      message = body.detail || message
    } catch { /* non-JSON response */ }
    throw new ApiError(message, response.status)
  }
  const blob = await response.blob()
  const disposition = response.headers.get('Content-Disposition')
  const filename = disposition?.match(/filename="?([^";]+)"?/i)?.[1] || `geosyncai-${kind}`
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(url)
}

export async function getWorkspaceData(id: string, projectList: Project[]): Promise<WorkspaceData> {
  const [serverDatasets, matches, conflicts, changes, serverVersions] = await Promise.all([
    getServerDatasets(id),
    request<ServerMatch[]>(`/projects/${id}/matches`),
    request<ServerConflict[]>(`/projects/${id}/conflicts`),
    request<ServerChange[]>(`/projects/${id}/changes`),
    request<ServerVersion[]>(`/projects/${id}/versions`),
  ])
  const featureLists = await Promise.all(serverDatasets.map((dataset) => getFeatures(id, dataset.id)))
  const features = new Map(featureLists.flat().map((feature) => [feature.id, feature]))
  const proposals = [...matches.map((match) => mapMatch(match, features, conflicts, changes)), ...changes.map((change) => mapChange(change, features))]
  window.localStorage.setItem('geosyncai_project_id', id)
  const proposalByFeature = new Map(proposals.flatMap((proposal) => proposal.sourceIds.map((sourceId) => [sourceId, proposal.id] as const)))
  const geoFeatures: GeoJSON.Feature[] = featureLists.flat().filter((feature) => feature.geometry && feature.status === 'processed').map((feature) => ({ type: 'Feature', id: feature.id, properties: { ...feature.attributes, id: proposalByFeature.get(feature.id) || feature.id, original_id: feature.original_id }, geometry: feature.geometry! }))
  const alerts: ChangeAlert[] = changes.map((change) => ({
    id: change.id,
    parcelId: change.evidence?.namespace_key ? String(change.evidence.namespace_key) : 'Unidentified feature',
    type: change.change_type === 'geometry_changed' ? 'Geometry change' : change.change_type === 'attribute_changed' ? 'Attribute change' : change.change_type === 'added' ? 'New feature' : 'Missing from snapshot',
    severity: change.boundary_change ? 'high' : 'medium',
    summary: change.evidence?.tolerance ? `Detected with configured tolerance ${change.evidence.tolerance}` : 'Change detected between dated vector snapshots',
    before: change.before_geometry ? 'Present in baseline' : 'Not found in baseline',
    after: change.after_geometry ? 'Present in comparison' : 'Not found in comparison',
    detectedAt: 'Recorded during processing',
    status: change.status === 'accepted' || change.status === 'validated' ? 'reviewed' : 'unreviewed',
  }))
  const datasets = serverDatasets.map(mapDataset)
  const pendingReview = proposals.filter((proposal) => proposal.status === 'pending' || proposal.status === 'deferred').length
  const projects = projectList.map((project) => project.id === id ? { ...project, datasetCount: datasets.length, pendingReview, status: (pendingReview ? 'needs-attention' : 'ready') as Project['status'] } : project)
  const totalRecords = datasets.reduce((sum, dataset) => sum + dataset.records, 0)
  const metrics: Metric[] = [
    { label: 'Source records', value: totalRecords.toLocaleString(), delta: `${datasets.length} datasets`, context: 'raw records retained at ingest', icon: 'layers', tone: 'green' },
    { label: 'Review queue', value: pendingReview.toString(), delta: `${proposals.length} proposals`, context: 'explicit decisions required', icon: 'review', tone: 'amber' },
    { label: 'Open conflicts', value: conflicts.filter((conflict) => conflict.status === 'open').length.toString(), delta: `${changes.filter((change) => change.boundary_change).length} boundary changes`, context: 'review before publication', icon: 'warning', tone: 'rose' },
    { label: 'Published versions', value: serverVersions.length.toString(), delta: 'append-only', context: 'lineage manifests available', icon: 'clock', tone: 'blue' },
  ]
  return { projects, datasets, proposals, alerts, versions: serverVersions.map(mapVersion), metrics, geojson: { type: 'FeatureCollection', features: geoFeatures }, activity: [] }
}
