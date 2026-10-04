import { request } from './api'

export type User = { id: string; username: string; role: string }
export type Project = { id: string; name: string; description?: string; owner_id: string }
export type Dataset = { id: string; name: string; capture_date?: string; uploaded_at: string; source_organization?: string;
  declared_crs?: string; normalized_crs?: string; analysis_crs?: string; content_hash: string; status: string; record_count: number;
  schema_mapping_version: number; validation_report: Record<string, unknown> }
export type Feature = { id: string; dataset_id?: string; dataset_name?: string; original_id?: string; attributes: Record<string, unknown>;
  canonical_attributes?: Record<string, unknown>;
  geometry: GeoJSON.Geometry | null; original_geometry: GeoJSON.Geometry | null; status: string; processing_reason?: string }
export type Mapping = { mapping: Record<string, string>; version: number; status: string; source_fields: { name: string }[] }
export type Evidence = { id: string; target: 'match' | 'change' | 'conflict'; revision: number; status: string; score?: number;
  left_feature_id?: string; right_feature_id?: string; source_feature_id?: string; comparison_feature_id?: string;
  before_geometry?: GeoJSON.Geometry; after_geometry?: GeoJSON.Geometry; change_type?: string;
  description?: string; evidence?: Record<string, unknown>; details?: Record<string, unknown> }
export type Parcel = { id: string; source_feature_ids: string[]; selection: { geometry_source_id?: string; attribute_source_id: string; attribute_sources?: Record<string,string>; revision: number; rationale: string } | null }
export type Version = { id: string; version: number; created_at: string; lineage_manifest: Record<string, unknown> }
export type Job = { id: string; status: string; error?: string; result?: Record<string, unknown>; job_type: string }
export const projectPath = (id: string) => `/projects/${id}`
export const get = <T,>(path: string) => request<T>(path)
export const post = <T,>(path: string, body?: unknown) => request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })
export async function upload(project: string, file: File, captureDate: string, source: string) {
  const body = new FormData()
  body.append('file', file)
  if (captureDate) body.append('capture_date', captureDate)
  if (source) body.append('source_organization', source)
  return request<Dataset>(`${projectPath(project)}/datasets/upload`, { method: 'POST', body })
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
