export type AppMode = 'api' | 'demo'
export type ProposalStatus = 'pending' | 'accepted' | 'rejected' | 'deferred'
export type ReviewAction = 'accept' | 'reject' | 'defer'

export interface Project { id: string; name: string; location: string; updatedAt: string; status: 'ready' | 'processing' | 'needs-attention'; datasetCount: number; pendingReview: number }
export interface Dataset { id: string; name: string; source: string; kind: 'Cadastral polygons' | 'Municipal polygons' | 'Revenue table' | 'Survey checkpoints'; format: 'GeoJSON' | 'CSV' | 'GeoPackage' | 'SHP ZIP'; records: number; crs: string; crsStatus: 'verified' | 'warning' | 'missing'; captureDate?: string; uploadedAt: string; status: 'ready' | 'processing' | 'needs-review'; issues: string[] }
export interface EvidenceMetric { label: string; value: string; tone: 'good' | 'warn' | 'neutral' }
export interface Proposal { id: string; targetType?: 'match' | 'change'; parcelId: string; sourceIds: string[]; title: string; subtitle: string; score: number; scoreLabel: string; status: ProposalStatus; boundaryConflict: boolean; geometry: GeoJSON.Polygon; evidence: EvidenceMetric[]; reasons: string[]; alternatives: { id: string; label: string; score: number; note: string }[]; conflicts: string[]; sourceValues: { source: string; identifier: string; area: string; updated: string }[]; reviewNote?: string }
export interface ChangeAlert { id: string; parcelId: string; type: 'Geometry change' | 'Attribute change' | 'New feature' | 'Missing from snapshot'; severity: 'high' | 'medium' | 'low'; summary: string; before: string; after: string; detectedAt: string; status: 'unreviewed' | 'reviewed' }
export interface Version { id: string; label: string; status: 'Published' | 'Validated' | 'In review' | 'Draft'; createdAt: string; createdBy: string; records: number; changeSummary: string }
export interface Metric { label: string; value: string; delta: string; context: string; icon: 'layers' | 'review' | 'warning' | 'clock'; tone: 'green' | 'amber' | 'blue' | 'rose' }
export interface Activity { id: string; type: 'upload' | 'review' | 'publish' | 'process'; title: string; detail: string; time: string }
export interface WorkspaceData { projects: Project[]; datasets: Dataset[]; proposals: Proposal[]; alerts: ChangeAlert[]; versions: Version[]; metrics: Metric[]; geojson: GeoJSON.FeatureCollection; activity: Activity[] }
