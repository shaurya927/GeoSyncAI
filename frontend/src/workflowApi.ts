import { request } from './api'

export type User = { id: string; username: string; role: string }
export type Project = { id: string; name: string; description?: string; owner_id: string; workflow_revision?: number; validated_revision?: number }
export type Dataset = { id: string; name: string; capture_date?: string; uploaded_at: string; source_organization?: string;
  declared_crs?: string; normalized_crs?: string; analysis_crs?: string; content_hash: string; status: string; record_count: number;
  schema_mapping_version: number; validation_report: Record<string, unknown>; access_classification?: string;
  license_classification?: string; accuracy_metadata?: Record<string, unknown>; provenance?: Record<string, unknown>;
  administrative_namespace?: Record<string, unknown>; source_version?: string; version_label?: string }
export type Feature = { id: string; dataset_id?: string; dataset_name?: string; original_id?: string; attributes: Record<string, unknown>;
  canonical_attributes?: Record<string, unknown>;
  geometry: GeoJSON.Geometry | null; original_geometry: GeoJSON.Geometry | null; status: string; processing_reason?: string }
export type Mapping = { mapping: Record<string, string>; version: number; status: string; source_fields: { name: string }[] }
export type Evidence = { id: string; target: 'match' | 'change' | 'conflict'; revision: number; status: string; score?: number;
  left_feature_id?: string; right_feature_id?: string; source_feature_id?: string; comparison_feature_id?: string;
  before_geometry?: GeoJSON.Geometry; after_geometry?: GeoJSON.Geometry; change_type?: string;
  description?: string; evidence?: Record<string, unknown>; details?: Record<string, unknown> }
export type Parcel = { id: string; source_feature_ids: string[]; geometry?: GeoJSON.Geometry | null; geometry_origin?: Record<string, unknown>;
  selection: { geometry_source_id?: string; attribute_source_id: string; attribute_sources?: Record<string,string>; revision: number; rationale: string } | null }
export type Version = { id: string; version: number; created_at: string; lineage_manifest: Record<string, unknown> }
export type Job = { id: string; status: string; error?: string; result?: Record<string, unknown>; job_type: string }
export type FieldAssignment = { id: string; assignee_id: string; parcel_entity_ids: string[]; expires_at?: string; status: string; revision: number; reference_policy: Record<string, unknown> }
export type FieldEvidence = { id: string; assignment_id: string; parcel_entity_id: string; client_event_id: string; payload: Record<string, unknown>; status: string }
export type GeometryChange = { id: string; operation: string; status: string; revision: number; parcel_entity_ids: string[]; predecessor_ids: string[]; successor_ids: string[];
  before_geometries: Record<string, GeoJSON.Geometry>; draft_geometries: Record<string, GeoJSON.Geometry>; approved_geometries: Record<string, GeoJSON.Geometry>;
  measurements: Record<string, unknown>; rationale: string; decision_rationale?: string }
export type ComplianceRule = { id: string; name: string; jurisdiction: string; category: string; version: number; status: string; inputs: Record<string, unknown>[]; formula: Record<string, unknown>; threshold: Record<string, unknown> }
export type CitizenGrant = { id: string; citizen_id: string; parcel_entity_id: string; published_version_id?: string; fields: string[]; expires_at?: string; status: string }
export type CitizenCase = { id: string; grant_id: string; parcel_entity_id: string; category: string; description: string; status: string; response?: string }
export type RasterAsset = { id: string; name: string; content_hash: string; mime_type: string; source_crs?: string; bounds?: number[]; metadata: Record<string, unknown>; attribution?: string; access_classification: string; status: string }
export type ModelArtifact = { id: string; version: string; model_type: string; metrics: Record<string, unknown>; activation_status?: string; artifact: Record<string, unknown> }
export type GroundControlSession = { id: string; dataset_id: string; method: string; source_crs?: string; target_crs?: string; control_points: Record<string, unknown>[]; residuals: Record<string, unknown>; status: string; revision: number; approved_dataset_id?: string }
export const projectPath = (id: string) => `/projects/${id}`
export const get = <T,>(path: string) => request<T>(path)
export const post = <T,>(path: string, body?: unknown) => request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })
export const patch = <T,>(path: string, body?: unknown) => request<T>(path, { method: 'PATCH', body: body === undefined ? undefined : JSON.stringify(body) })
export async function datasetFeatures(project: string, dataset: string): Promise<Feature[]> {
  const result: Feature[] = []
  for (let offset=0; ; offset+=1000) {
    const page = await get<Feature[]>(`${projectPath(project)}/datasets/${dataset}/features?limit=1000&offset=${offset}`)
    result.push(...page)
    if (page.length < 1000) return result
  }
}
export async function upload(project: string, file: File, captureDate: string, source: string, metadata: { license?: string; sourceVersion?: string; namespace?: string; accessClassification?: string; accuracy?: string; provenance?: string } = {}) {
  const body = new FormData()
  body.append('file', file)
  if (captureDate) body.append('capture_date', captureDate)
  if (source) body.append('source_organization', source)
  if (metadata.license) body.append('license_classification', metadata.license)
  if (metadata.sourceVersion) body.append('source_version', metadata.sourceVersion)
  if (metadata.namespace) body.append('administrative_namespace', metadata.namespace)
  if (metadata.accessClassification) body.append('access_classification', metadata.accessClassification)
  if (metadata.accuracy) body.append('accuracy_metadata', metadata.accuracy)
  if (metadata.provenance) body.append('provenance', metadata.provenance)
  return request<Dataset>(`${projectPath(project)}/datasets/upload`, { method: 'POST', body })
}
export async function uploadRaster(project: string, file: File, sourceCrs: string, attribution: string) {
  const body = new FormData(); body.append('file', file); if (sourceCrs) body.append('source_crs', sourceCrs); if (attribution) body.append('attribution', attribution)
  return request<RasterAsset>(`${projectPath(project)}/raster-assets`, { method: 'POST', body })
}
export async function runJob(project: string, type: string, payload: unknown, report: (job: Job) => void) {
  let job = await post<Job>(`${projectPath(project)}/jobs?job_type=${type}`, payload)
  report(job)
  while (job.status === 'queued' || job.status === 'running') {
    await new Promise((resolve) => window.setTimeout(resolve, 500))
    job = await get<Job>(`${projectPath(project)}/jobs/${job.id}`)
    report(job)
  }
  if (job.status !== 'succeeded') throw new Error(job.error || 'Processing failed')
  return job
}
