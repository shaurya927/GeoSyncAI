import * as w from './workflowApi'
import { ApiError } from './api'
import { accessToken, clearAccessToken } from './credentials'

export type Capabilities = { project_id: string; read_departmental: boolean; process: boolean; review: boolean; publish: boolean; fieldwork: boolean; citizen_records: boolean; exports: boolean }
type Snapshot = { version: 1; user: w.User; projects: w.Project[]; project: string; verifiedAt: number; expiresAt: number; capabilities: Record<string, Capabilities> }
const KEY = 'geosyncai-session-v1'
const OFFLINE_WINDOW = 8 * 60 * 60 * 1000
export function tokenExpiry(): number {
  try { const token = accessToken(); return Number(JSON.parse(atob(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/'))).exp) * 1000 }
  catch { return 0 }
}
export function cachedSession(): Snapshot | undefined {
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) || 'null') as Snapshot | null
    if (saved?.version === 1 && saved.user.role === 'field' && saved.expiresAt > Date.now() && tokenExpiry() > Date.now() && saved.verifiedAt <= Date.now()) return saved
  } catch { /* corrupted or unavailable storage never authorizes access */ }
}
export function saveSession(user: w.User, projects: w.Project[], project: string, capabilities: Record<string, Capabilities>, verifiedAt = Date.now()) {
  // Cache only minimal account/project metadata, never department datasets or citizen records.
  try { localStorage.setItem(KEY, JSON.stringify({ version: 1, user, projects: projects.map(p => ({ id: p.id, name: p.name, owner_id: p.owner_id })), project, capabilities, verifiedAt, expiresAt: Math.min(tokenExpiry(), verifiedAt + OFFLINE_WINDOW) })) }
  catch { /* online operation remains available; the field panel reports persistence failures */ }
}
export function clearSession() { clearAccessToken(); try { localStorage.removeItem(KEY) } catch { /* unavailable storage is already inaccessible */ } }
export async function verifyAccount(expected: w.User, project: string) {
  const user = await w.get<w.User>('/auth/me')
  if (user.id !== expected.id) throw new ApiError('Account changed. Sign in as the owner of these drafts.', 401)
  const capabilities = await w.get<Capabilities>(`${w.projectPath(project)}/capabilities`)
  if (!capabilities.fieldwork) throw new ApiError('Fieldwork permission was revoked. Queued evidence remains retained.', 403)
  return { user, capabilities }
}
export const activeAssignment = (item: w.FieldAssignment, user: w.User) => item.assignee_id === user.id && ['assigned', 'active'].includes(item.status) && (!item.expires_at || new Date(item.expires_at).getTime() > Date.now())
