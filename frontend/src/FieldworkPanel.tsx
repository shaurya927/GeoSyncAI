import { useCallback, useEffect, useRef, useState } from 'react'
import * as w from './workflowApi'
import { ApiError } from './api'
import { activeAssignment, cachedSession, verifyAccount } from './session'

type Draft = { assignment_id: string; parcel_entity_id: string; client_event_id: string; expected_project_revision: number; payload: Record<string, unknown>; server_evidence_id?: string }
type Reference = { assignment_id: string; features: { parcel_entity_id: string; attributes: Record<string, unknown>; geometry: GeoJSON.Geometry | null }[] }
export default function FieldworkPanel({ project, user, run }: { project: string; user: w.User; run: (action: () => Promise<void>) => void }) {
  const prefix = `geosyncai-field-${project}-${user.id}`
  // Keep the existing queue key so updates preserve already captured evidence.
  const queueKey = `geosyncai-field-queue-${project}-${user.id}`
  const assignmentKey = `geosyncai-field-assignments-${project}-${user.id}`
  const revisionKey = `${prefix}-revision`
  const [assignments, setAssignments] = useState<w.FieldAssignment[]>([])
  const [selectedId, setSelectedId] = useState(''); const [parcel, setParcel] = useState('')
  const [queue, setQueue] = useState<Draft[]>([]); const queueRef = useRef(queue); queueRef.current = queue
  const [references, setReferences] = useState<Record<string, Reference>>({})
  const [note, setNote] = useState(''); const [photo, setPhoto] = useState<string>()
  const [online, setOnline] = useState(navigator.onLine); const [message, setMessage] = useState('')
  const [resolutionReason, setResolutionReason] = useState('')
  const syncing = useRef(false)
  const read = <T,>(key: string, fallback: T): T => { try { return JSON.parse(localStorage.getItem(key) || 'null') as T || fallback } catch { return fallback } }
  const store = useCallback((key: string, value: unknown) => { try { localStorage.setItem(key, JSON.stringify(value)); return true } catch { setMessage('Device storage is unavailable or full. Export retained drafts before clearing space.'); return false } }, [])
  const persist = useCallback((next: Draft[]) => {
    if (next.length > 50 || new Blob([JSON.stringify(next)]).size > 10 * 1024 * 1024) { setMessage('Device queue limit: 50 drafts / 10 MB. Export or sync retained evidence first.'); return false }
    if (!store(queueKey, next)) return false
    queueRef.current = next; setQueue(next); return true
  }, [queueKey, store])
  const load = useCallback(async () => {
    try {
      await verifyAccount(user, project)
      const [items, state] = await Promise.all([w.get<w.FieldAssignment[]>(`${w.projectPath(project)}/field-assignments`), w.get<w.Project>(w.projectPath(project))])
      const allowed = items.filter(item => activeAssignment(item, user))
      setAssignments(allowed); store(assignmentKey, allowed); store(revisionKey, state.workflow_revision || 0)
      const refs: Record<string, Reference> = {}
      for (const item of allowed) { refs[item.id] = await w.get<Reference>(`${w.projectPath(project)}/field-assignments/${item.id}/reference`) }
      setReferences(refs); store(`${prefix}-references`, refs)
      return allowed
    } catch (error) {
      const saved = cachedSession()
      if (error instanceof ApiError && error.status === 0 && !navigator.onLine && saved?.user.id === user.id && saved.capabilities[project]?.fieldwork) {
        const allowed = read<w.FieldAssignment[]>(assignmentKey, []).filter(item => activeAssignment(item, user))
        const refs = read<Record<string, Reference>>(`${prefix}-references`, {})
        setAssignments(allowed); setReferences(Object.fromEntries(allowed.map(item => [item.id, refs[item.id]]).filter(([, ref]) => ref)))
        setMessage('Offline field session · previously verified assignments only. Reconnect before sync.'); return allowed
      }
      setAssignments([]); setReferences({})
      if (error instanceof ApiError && [401, 403].includes(error.status)) { localStorage.removeItem(assignmentKey); localStorage.removeItem(`${prefix}-references`) }
      throw error
    }
  }, [project, user, assignmentKey, revisionKey, prefix, store])
  useEffect(() => { setQueue(read<Draft[]>(queueKey, [])); void load().catch(e => setMessage(String(e))) }, [queueKey, load])
  useEffect(() => { if (!assignments.some(item => item.id === selectedId)) { setSelectedId(assignments[0]?.id || ''); setParcel('') } }, [assignments, selectedId])
  // Expired cached assignments disappear even while the tab remains offline.
  useEffect(() => { const timer = window.setInterval(() => setAssignments(items => items.filter(item => activeAssignment(item, user))), 15000); return () => clearInterval(timer) }, [user])
  const sync = useCallback(async () => {
    if (!navigator.onLine || syncing.current) return
    syncing.current = true
    try {
      const allowed = await load()
      for (const draft of [...queueRef.current]) {
        if (draft.server_evidence_id || !allowed.some(item => item.id === draft.assignment_id && item.parcel_entity_ids.includes(draft.parcel_entity_id))) continue
        try {
          const result = await w.post<w.FieldEvidence>(`${w.projectPath(project)}/field-assignments/${draft.assignment_id}/evidence`, draft)
          const next = result.status === 'revision_conflict' ? queueRef.current.map(item => item.client_event_id === draft.client_event_id ? { ...item, server_evidence_id: result.id } : item) : queueRef.current.filter(item => item.client_event_id !== draft.client_event_id)
          if (!persist(next)) break
        } catch (error) { setMessage(`Sync stopped; evidence retained. ${String(error)}`); break }
      }
      setMessage(queueRef.current.length ? 'Retained drafts need retry, an active assignment, or explicit revision review.' : 'All queued evidence synced once. It remains pending officer review.')
    } finally { syncing.current = false }
  }, [load, project, persist])
  useEffect(() => { const on = () => { setOnline(true); run(sync) }; const off = () => setOnline(false); window.addEventListener('online', on); window.addEventListener('offline', off); return () => { window.removeEventListener('online', on); window.removeEventListener('offline', off) } }, [run, sync])
  const submit = () => run(async () => {
    const selected = assignments.find(item => item.id === selectedId)
    if (!selected || !activeAssignment(selected, user) || !selected.parcel_entity_ids.includes(parcel) || !note.trim()) throw new Error('Choose an active assigned parcel and enter a field note')
    if (!navigator.onLine && cachedSession()?.user.id !== user.id) throw new Error('Offline session expired. Reconnect and sign in; existing drafts remain retained.')
    const position = await new Promise<GeolocationPosition | undefined>(resolve => { if (!navigator.geolocation) return resolve(undefined); navigator.geolocation.getCurrentPosition(resolve, () => resolve(undefined), { enableHighAccuracy: true, timeout: 5000 }) })
    const draft: Draft = { assignment_id: selected.id, parcel_entity_id: parcel, client_event_id: crypto.randomUUID(), expected_project_revision: read<number>(revisionKey, Number(localStorage.getItem(`geosyncai-project-revision-${project}`) || 0)), payload: { note, photo_data_url: photo || null, captured_at: new Date().toISOString(), location: position ? { latitude: position.coords.latitude, longitude: position.coords.longitude, accuracy_m: position.coords.accuracy, timestamp: position.timestamp } : null } }
    // Persist first: connection loss or an HTTP rejection cannot discard captured evidence.
    if (!persist([...queueRef.current, draft])) return
    setNote(''); setPhoto(undefined)
    if (navigator.onLine) await sync()
    else setMessage('Stored in this account’s browser device queue. Reopen Fieldwork while offline; reconnect to sync.')
  })
  const resolve = (draft: Draft) => run(async () => {
    if (!resolutionReason.trim() || !draft.server_evidence_id) throw new Error('Enter a rationale for revision resubmission')
    await load()
    const latest = await w.get<w.Project>(w.projectPath(project))
    const result = await w.post<w.FieldEvidence>(`${w.projectPath(project)}/field-assignments/${draft.assignment_id}/evidence/${draft.server_evidence_id}/resolve`, { expected_revision: draft.expected_project_revision, decision: 'resubmit', rationale: resolutionReason, new_expected_project_revision: latest.workflow_revision || 0 })
    persist(result.status === 'submitted' ? queueRef.current.filter(item => item.client_event_id !== draft.client_event_id) : queueRef.current.map(item => item.client_event_id === draft.client_event_id ? { ...item, server_evidence_id: result.id, expected_project_revision: latest.workflow_revision || 0 } : item))
  })
  const exportQueue = () => { const url = URL.createObjectURL(new Blob([JSON.stringify(queue, null, 2)], { type: 'application/json' })); const a = document.createElement('a'); a.href = url; a.download = `geosyncai-field-${project}-${user.id}.json`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000) }
  return <section className="panel inspector"><h2>Authorized fieldwork</h2><p>Bounded assignments only · <strong>{online ? 'Online' : 'Offline'}</strong> · queued drafts: {queue.length}</p><p>Device drafts and photos are browser storage, not an encrypted vault. Sign out locks access and retains unsynced evidence for the same account.</p>{message && <p role="status" className="notice-banner">{message}</p>}
    {!assignments.length && <p>No current authorized assignment is available. Retained drafts can still be exported or deliberately cleared.</p>}
    {assignments.length > 0 && <><label>Assignment<select aria-label="Assignment" value={selectedId} onChange={e => { setSelectedId(e.target.value); setParcel('') }}>{assignments.map(item => <option key={item.id} value={item.id}>{item.id.slice(0, 8)} · {item.status}</option>)}</select></label><label>Assigned parcel<select aria-label="Assigned parcel" value={parcel} onChange={e => setParcel(e.target.value)}><option value="">Choose parcel</option>{assignments.find(item => item.id === selectedId)?.parcel_entity_ids.map(id => <option key={id}>{id}</option>)}</select></label><details><summary>Authorized reference layer</summary><pre className="json-preview">{JSON.stringify(references[selectedId] || { message: 'Reference not downloaded; reconnect' }, null, 2)}</pre></details><label>Field note<textarea aria-label="Field note" value={note} onChange={e => setNote(e.target.value)} /></label><label>Photo evidence<input type="file" accept="image/*" capture="environment" onChange={e => { const file = e.target.files?.[0]; if (!file) return; if (file.size > 5 * 1024 * 1024) { setMessage('Photo exceeds 5 MB'); return } const reader = new FileReader(); reader.onload = () => setPhoto(String(reader.result)); reader.onerror = () => setMessage('Photo could not be read'); reader.readAsDataURL(file) }} /></label><button className="button button-primary" disabled={!parcel || !note.trim()} onClick={submit}>{online ? 'Submit evidence' : 'Save offline draft'}</button></>}
    {queue.length > 0 && <><button className="button button-secondary" disabled={!online} onClick={() => run(sync)}>Retry queued sync</button><button className="button button-secondary" onClick={exportQueue}>Export queued drafts</button>{queue.some(item => item.server_evidence_id) && <label>Revision resubmission rationale<input value={resolutionReason} onChange={e => setResolutionReason(e.target.value)} /></label>}{queue.map(item => <div className="source-row" key={item.client_event_id}><span>{item.parcel_entity_id} · {item.server_evidence_id ? 'Revision conflict' : 'Queued'}</span>{item.server_evidence_id && <button className="button button-secondary" disabled={!online || !resolutionReason.trim()} onClick={() => resolve(item)}>Resolve revision conflict</button>}</div>)}</>}
    <button className="button button-secondary" onClick={() => { if (confirm('Delete this account’s cached assignments and drafts? Export unresolved evidence first.')) { [queueKey, assignmentKey, revisionKey, `${prefix}-references`].forEach(key => localStorage.removeItem(key)); setQueue([]); queueRef.current = []; setAssignments([]); setReferences({}); setMessage('This account’s device data was deliberately cleared.') } }}>Clear this account’s device data</button>
  </section>
}
