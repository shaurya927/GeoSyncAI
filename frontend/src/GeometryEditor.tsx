import { useCallback, useEffect, useRef, useState } from 'react'
import * as maplibregl from './maplibre'
import * as w from './workflowApi'

type Geometries = Record<string, GeoJSON.Geometry>
type Preview = { draft_geometries: Geometries; original_geometries: Geometries; before_fingerprint: string; measurements: Record<string, unknown>; analysis_crs: string; gates: string[] }
type Vertex = { id: string; path: number[]; point: GeoJSON.Position }
function vertices(items: Geometries): Vertex[] {
  const result: Vertex[] = []
  for (const [id, geometry] of Object.entries(items)) {
    const visit = (coordinates: unknown, path: number[]) => {
      if (!Array.isArray(coordinates)) return
      if (Array.isArray(coordinates[0]) && typeof coordinates[0][0] === 'number') {
        // Closed-ring duplicate is updated together with vertex zero.
        coordinates.slice(0, -1).forEach((point, i) => result.push({ id, path: [...path, i], point }))
      } else coordinates.forEach((value, i) => visit(value, [...path, i]))
    }
    if (geometry.type === 'Polygon' || geometry.type === 'MultiPolygon') visit(geometry.coordinates, [])
  }
  return result
}
function same(a: GeoJSON.Position, b: GeoJSON.Position) { return Math.abs(a[0] - b[0]) < 1e-10 && Math.abs(a[1] - b[1]) < 1e-10 }
function moveVertex(items: Geometries, vertex: Vertex, target: GeoJSON.Position, shared: boolean): Geometries {
  const next = structuredClone(items)
  if (shared) {
    const replace = (value: unknown): void => { if (!Array.isArray(value)) return; if (typeof value[0] === 'number') { if (same(value, vertex.point)) { value[0] = target[0]; value[1] = target[1] } } else value.forEach(replace) }
    Object.values(next).forEach(g => { if ('coordinates' in g) replace(g.coordinates) })
  } else {
    const geometry = next[vertex.id]; if (!('coordinates' in geometry)) return next
    let ring = geometry.coordinates as unknown as unknown[]
    for (const index of vertex.path.slice(0, -1)) ring = ring[index] as unknown[]
    const index = vertex.path[vertex.path.length - 1]; ring[index] = target
    if (index === 0) ring[ring.length - 1] = [...target]
  }
  return next
}
// Local metric approximation is used only as a UI snapping aid. The server
// projects geometry into its analysis CRS for authoritative measurements.
function snap(point: GeoJSON.Position, candidates: Geometries, tolerance: number, exclude?: Vertex): GeoJSON.Position {
  let closest = point; let best = tolerance
  const sx = 111320 * Math.cos(point[1] * Math.PI / 180), sy = 110574
  const consider = (candidate: GeoJSON.Position) => { const distance = Math.hypot((candidate[0]-point[0])*sx, (candidate[1]-point[1])*sy); if (distance < best) { best = distance; closest = candidate } }
  const rings = (value: unknown): void => {
    if (!Array.isArray(value)) return
    if (Array.isArray(value[0]) && typeof value[0][0] === 'number') {
      const ring = value as GeoJSON.Position[]
      ring.forEach(p => { if (!exclude || !same(p, exclude.point)) consider(p) })
      for (let i=0; i<ring.length-1; i++) {
        const a=ring[i], b=ring[i+1]; if (exclude && (same(a, exclude.point) || same(b, exclude.point))) continue
        const dx=(b[0]-a[0])*sx, dy=(b[1]-a[1])*sy, denominator=dx*dx+dy*dy
        if (!denominator) continue
        const t=Math.max(0, Math.min(1, (((point[0]-a[0])*sx)*dx+((point[1]-a[1])*sy)*dy)/denominator))
        consider([a[0]+t*(b[0]-a[0]), a[1]+t*(b[1]-a[1])])
      }
    } else value.forEach(rings)
  }
  Object.values(candidates).forEach(g => { if ('coordinates' in g) rings(g.coordinates) }); return closest
}
function EditMap({ originals, draft, neighbors, cut, drawing, editable, shared, tolerance, onCut, onEdit }: { originals: Geometries; draft: Geometries; neighbors: Geometries; cut: GeoJSON.Position[]; drawing: boolean; editable: boolean; shared: boolean; tolerance: number; onCut: (point: GeoJSON.Position) => void; onEdit: (next: Geometries) => void }) {
  const host = useRef<HTMLDivElement>(null); const [map, setMap] = useState<maplibregl.Map>(); const [error, setError] = useState('')
  const current = useRef({ draft, neighbors, drawing, tolerance, onCut, onEdit, shared }); current.current = { draft, neighbors, drawing, tolerance, onCut, onEdit, shared }
  useEffect(() => {
    if (!host.current) return
    let instance: maplibregl.Map | undefined
    try {
      instance = new maplibregl.Map({ container: host.current, center: [73,20], zoom: 12, style: { version: 8, sources: {}, layers: [{ id: 'background', type: 'background', paint: { 'background-color': '#e8efe9' } }] } })
      instance.addControl(new maplibregl.NavigationControl()); instance.addControl(new maplibregl.ScaleControl({ unit: 'metric' }))
      instance.on('load', () => setMap(instance)); instance.on('click', event => { const state = current.current; if (state.drawing) state.onCut(snap([event.lngLat.lng, event.lngLat.lat], state.neighbors, state.tolerance)) })
      const observer = new ResizeObserver(() => instance?.resize()); observer.observe(host.current)
      return () => { observer.disconnect(); instance?.remove() }
    } catch { instance?.remove(); setError('Map could not start. Enable WebGL or use the coordinate editor below.') }
  }, [])
  useEffect(() => {
    if (!map) return
    const data: GeoJSON.FeatureCollection = { type: 'FeatureCollection', features: [] }
    for (const [kind, items] of Object.entries({ neighbor: neighbors, before: originals, draft })) for (const [id, geometry] of Object.entries(items)) data.features.push({ type: 'Feature', geometry, properties: { id, kind } })
    if (cut.length >= 2) data.features.push({ type: 'Feature', geometry: { type: 'LineString', coordinates: cut }, properties: { kind: 'cut' } })
    const source = map.getSource('edits') as maplibregl.GeoJSONSource | undefined
    if (source) source.setData(data)
    else {
      map.addSource('edits', { type: 'geojson', data })
      map.addLayer({ id: 'edit-fill', type: 'fill', source: 'edits', paint: { 'fill-color': ['match', ['get','kind'], 'draft','#3c7488','before','#b87a1a','#4f9981'], 'fill-opacity': .23 } })
      map.addLayer({ id: 'edit-line', type: 'line', source: 'edits', paint: { 'line-color': ['match',['get','kind'],'cut','#e58732','draft','#3c7488','before','#b87a1a','#4f9981'], 'line-width': ['match',['get','kind'],'cut',4,2] } })
    }
    const markers: maplibregl.Marker[] = []
    if (editable && !drawing) for (const vertex of vertices(draft)) {
      const element = document.createElement('button'); element.className = 'vertex-handle'; element.title = `Drag vertex ${vertex.path.join('.')} of ${vertex.id}`; element.setAttribute('aria-label', element.title)
      const marker = new maplibregl.Marker({ element, draggable: true }).setLngLat([vertex.point[0], vertex.point[1]]).addTo(map)
      marker.on('dragend', () => { const state=current.current; const p=marker.getLngLat(); const position=snap([p.lng,p.lat], { ...state.neighbors, ...state.draft }, state.tolerance, vertex); state.onEdit(moveVertex(state.draft, vertex, position, state.shared)) })
      markers.push(marker)
    }
    return () => markers.forEach(marker => marker.remove())
  }, [map, originals, draft, neighbors, cut, editable, drawing])
  useEffect(() => {
    if (!map) return
    const points = vertices(originals).map(v => v.point); const bounds=new maplibregl.LngLatBounds(); points.forEach(p => bounds.extend([p[0],p[1]])); if (!bounds.isEmpty()) map.fitBounds(bounds, { padding: 60, maxZoom: 19, duration: 0 })
  }, [map, originals])
  return <><div ref={host} className="parcel-map-canvas" role="region" aria-label="Interactive boundary editor" data-editor-ready={!!map} />{error && <p role="alert">{error}</p>}<p className="map-legend-static">Amber: approved boundary · Blue: proposed boundary · Green: neighbors · Orange: drawn cut · Drag blue handles to edit vertices.</p></>
}

export default function GeometryEditor({ project, parcels, writable, canReview, refresh, run }: { project: string; parcels: w.Parcel[]; writable: boolean; canReview: boolean; refresh: () => Promise<void>; run: (action: () => Promise<void>) => void }) {
  const [operation, setOperation] = useState<'move'|'split'|'merge'|'shared_edge'>('move')
  const [participants, setParticipants] = useState<string[]>([]); const [originals, setOriginals] = useState<Geometries>({}); const [draft, setDraft] = useState<Geometries>({})
  const [preview, setPreview] = useState<Preview>(); const [history, setHistory] = useState<Geometries[]>([]); const [cut, setCut] = useState<GeoJSON.Position[]>([]); const [drawing, setDrawing] = useState(false)
  const [rationale, setRationale] = useState(''); const [attribute, setAttribute] = useState(''); const [changes, setChanges] = useState<w.GeometryChange[]>([])
  const [decisions, setDecisions] = useState<Record<string,string>>({}); const [tolerance, setTolerance] = useState(.5)
  const [vertexIndex, setVertexIndex] = useState(0); const [longitude, setLongitude] = useState(''); const [latitude, setLatitude] = useState(''); const [json, setJson] = useState('')
  const load=useCallback(async () => setChanges(await w.get<w.GeometryChange[]>(`${w.projectPath(project)}/geometry-changes`)),[project])
  useEffect(() => { run(load); void w.get<{ geometry_tolerance?: number }>(`${w.projectPath(project)}/policy`).then(p => setTolerance(p.geometry_tolerance || .5)).catch(() => undefined) },[load,project,run])
  const neighbors=Object.fromEntries(parcels.filter(p => p.geometry && !participants.includes(p.id)).map(p => [p.id,p.geometry!]))
  const selected=parcels.filter(p => participants.includes(p.id)); const sources=[...new Set(selected.flatMap(p => p.source_feature_ids))]
  const reset=() => { setDraft({}); setOriginals({}); setPreview(undefined); setHistory([]); setCut([]); setDrawing(false); setAttribute('') }
  const edit=(next: Geometries) => { setHistory(previous => [...previous.slice(-29),draft]); setDraft(next); setPreview(undefined); setJson(JSON.stringify(next,null,2)) }
  const choose=(id: string) => { reset(); setParticipants(current => current.includes(id) ? current.filter(value => value !== id) : ['move','split'].includes(operation) ? [id] : [...current,id]) }
  const generate=() => run(async () => {
    if (!participants.length) throw new Error('Choose approved parcel boundaries')
    const result=await w.post<Preview>(`${w.projectPath(project)}/geometry-drafts`,{ operation,parcel_entity_ids:participants,cut_line:operation==='split' && cut.length>=2 ? { type:'LineString',coordinates:cut } : undefined, draft_geometries:['move','shared_edge'].includes(operation) ? draft : {}, before_geometries:originals })
    setHistory(previous => [...previous.slice(-29),draft]); setDraft(result.draft_geometries); setOriginals(result.original_geometries); setJson(JSON.stringify(result.draft_geometries,null,2)); setPreview(result); setDrawing(false)
  })
  const submit=() => run(async () => {
    if (!preview || !rationale.trim()) throw new Error('Preview the current draft and enter a rationale')
    if (['split','merge'].includes(operation) && !attribute) throw new Error('Choose the reviewed attribute source for successors')
    await w.post(`${w.projectPath(project)}/geometry-changes`,{ operation,parcel_entity_ids:participants,draft_geometries:draft,expected_geometry_fingerprint:preview.before_fingerprint,attribute_source_id:attribute || undefined,rationale })
    reset(); setParticipants([]); setRationale(''); await load(); await refresh()
  })
  const decide=(change: w.GeometryChange,decision:string) => run(async () => { const reason=decisions[change.id]?.trim(); if (!reason) throw new Error('Enter a review rationale'); await w.post(`${w.projectPath(project)}/geometry-changes/${change.id}/decision`,{ decision,expected_revision:change.revision,rationale:reason }); await load(); await refresh() })
  const allVertices=vertices(draft); const vertex=allVertices[vertexIndex]
  useEffect(() => { const current=vertices(draft)[vertexIndex]; if(current) { setLongitude(String(current.point[0])); setLatitude(String(current.point[1])) } },[draft,vertexIndex])
  return <><section className="panel inspector"><h2>Boundary editor</h2><p>Edit approved polygon boundaries with their holes intact. Split intersects the drawn cut with the parcel; merge unions selected boundaries. Every edit needs a measured preview and reviewer decision before publication.</p>
    <label>Operation<select value={operation} onChange={e => { setOperation(e.target.value as typeof operation); setParticipants([]); reset() }}><option value="move">Move / vertex edit</option><option value="split">Split one parcel</option><option value="merge">Merge selected parcels</option><option value="shared_edge">Shared-edge edit</option></select></label>
    <div className="record-preview">{parcels.map(p => <label key={p.id}><input type="checkbox" disabled={!p.geometry || !writable} checked={participants.includes(p.id)} onChange={() => choose(p.id)} /> {p.id.slice(0,12)} · {p.geometry_origin?.kind ? String(p.geometry_origin.kind) : 'geometry unavailable'}</label>)}</div>
    <div className="title-actions"><button className="button button-secondary" disabled={!writable || !participants.length} onClick={generate}>{Object.keys(draft).length ? 'Validate current preview' : 'Create draft geometry'}</button><button className="button button-secondary" disabled={!history.length} onClick={() => { const previous=history[history.length-1]; setHistory(history.slice(0,-1)); setDraft(previous); setJson(JSON.stringify(previous,null,2)); setPreview(undefined) }}>Undo draft edit</button><button className="button button-secondary" onClick={reset}>Cancel draft</button></div>
    {operation==='split' && <><button className="button button-secondary" disabled={!writable || !participants.length} onClick={() => { setCut([]); setDrawing(true); setPreview(undefined); if (!Object.keys(originals).length) setOriginals(Object.fromEntries(selected.filter(p => p.geometry).map(p => [p.id,p.geometry!]))) }}>Draw cut on map</button><p>{drawing ? `Click outside one edge, then across and outside the opposite edge (${cut.length} points). Click Validate to intersect the real parcel.` : 'Automatic cut is available through Create draft. Draw a cut for a specific division.'}</p></>}
    {(Object.keys(originals).length>0 || Object.keys(draft).length>0) && <EditMap originals={originals} draft={draft} neighbors={neighbors} cut={cut} drawing={drawing} editable={writable && ['move','shared_edge'].includes(operation)} shared={operation==='shared_edge'} tolerance={tolerance} onCut={point => { setCut(points => [...points,point]); setPreview(undefined) }} onEdit={edit} />}
    {['move','shared_edge'].includes(operation) && allVertices.length>0 && <details><summary>Accessible vertex coordinates and snapping</summary><p>Vertices snap to nearby vertices/edges within {tolerance} m. Shared-edge mode moves coincident vertices in every selected participant together; include both neighbors.</p><label>Vertex<select aria-label="Vertex" value={vertexIndex} onChange={e => { const index=Number(e.target.value); setVertexIndex(index); setLongitude(String(allVertices[index].point[0])); setLatitude(String(allVertices[index].point[1])) }}>{allVertices.map((v,i) => <option key={`${v.id}-${v.path.join('.')}`} value={i}>{v.id.slice(0,8)} · vertex {v.path.join('.')} · {v.point[0]}, {v.point[1]}</option>)}</select></label><label>Vertex longitude<input type="number" step="any" value={longitude} onChange={e => setLongitude(e.target.value)} /></label><label>Vertex latitude<input type="number" step="any" value={latitude} onChange={e => setLatitude(e.target.value)} /></label><button className="button button-secondary" disabled={!writable || !longitude || !latitude} onClick={() => { if (vertex) edit(moveVertex(draft,vertex,snap([Number(longitude),Number(latitude)],{...neighbors,...draft},tolerance,vertex),operation==='shared_edge')) }}>Apply vertex with snapping</button></details>}
    <details><summary>Advanced draft GeoJSON</summary><label>Draft GeoJSON<textarea rows={8} value={json} onChange={e => setJson(e.target.value)} /></label><button className="button button-secondary" disabled={!writable || ['split','merge'].includes(operation)} onClick={() => run(async () => edit(JSON.parse(json) as Geometries))}>Apply JSON draft</button></details>
    {preview && <section aria-label="Measured geometry preview">{preview.gates.map(gate => <p key={gate} role="alert" className="error-banner">{gate}</p>)}<h3>Measured preview · {preview.analysis_crs}</h3><div className="metric-grid">{['area_conservation_delta_m2','partition_difference_m2','draft_overlap_m2','max_displacement_m'].map(key => <div className="metric-card" key={key}><strong>{Number(preview.measurements[key] || 0).toFixed(3)}</strong><p>{key.replace(/_/g,' ')}</p></div>)}</div><details><summary>Areas and affected neighbors</summary><pre className="json-preview">{JSON.stringify(preview.measurements,null,2)}</pre></details></section>}
    {['split','merge'].includes(operation) && <label>Successor attribute source<select value={attribute} onChange={e => setAttribute(e.target.value)}><option value="">Choose explicit predecessor evidence</option>{sources.map(id => <option key={id}>{id}</option>)}</select></label>}
    <label>Submission rationale<input required value={rationale} onChange={e => setRationale(e.target.value)} /></label><button className="button button-primary" disabled={!writable || !preview || preview.gates.length > 0 || !rationale.trim()} onClick={submit}>Submit geometry draft</button>
  </section><section className="panel inspector"><h3>Submitted geometry changes</h3>{!changes.length && <p>No geometry drafts have been submitted.</p>}{changes.map(change => <details key={change.id} open={['draft','deferred'].includes(change.status)}><summary>{change.operation} · {change.status} · revision {change.revision}</summary><pre className="json-preview">{JSON.stringify(change,null,2)}</pre>{['draft','deferred'].includes(change.status) && <><label>Geometry review rationale<input value={decisions[change.id] || ''} onChange={e => setDecisions({...decisions,[change.id]:e.target.value})} /></label><div className="title-actions">{['approved','deferred','rejected'].map(decision => <button className="button button-secondary" key={decision} disabled={!canReview || !decisions[change.id]?.trim()} onClick={() => decide(change,decision)}>{decision==='approved'?'Approve':decision==='rejected'?'Reject':'Defer'}</button>)}</div></>}</details>)}</section></>
}
