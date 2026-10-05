import { useCallback, useEffect, useRef, useState } from 'react'
import maplibregl from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import * as api from './api'
import * as w from './workflowApi'

type View = 'overview' | 'datasets' | 'review' | 'alerts' | 'versions'
const titles: Record<View, string> = { overview: 'Overview', datasets: 'Datasets', review: 'Review queue', alerts: 'Change alerts', versions: 'Versions & history' }
const canonicalFields = ['parcel_id', 'survey_number', 'property_account', 'village', 'village_code', 'district', 'ward', 'recorded_area', 'area_units']
const format = (value: unknown) => JSON.stringify(value, null, 2)
const label = (feature?: w.Feature) => feature?.original_id || feature?.id || 'Unavailable'
function Brand() { return <div className="brand"><span className="brand-symbol"><span /><span /><span /><span /></span>GeoSync<span className="brand-accent">AI</span></div> }
function Json({ value }: { value: unknown }) { return <pre className="json-preview">{format(value)}</pre> }

function PolicyEditor({ project, enabled, run }: { project: string; enabled: boolean; run: (action: () => Promise<void>) => void }) {
  const [policy, setPolicy] = useState('')
  useEffect(() => { run(async () => { setPolicy(format(await w.get(`${w.projectPath(project)}/policy`))) }) }, [project, run])
  return <details><summary>Versioned processing policy</summary><form onSubmit={e => { e.preventDefault(); run(async () => {
    const current = JSON.parse(policy) as Record<string, unknown>
    setPolicy(format(await w.post(`${w.projectPath(project)}/policy`, { ...current, expected_version: current.version })))
  }) }}><p>Distances and tolerances use metres; overlap tolerance uses square metres. Optional coverage_boundary is a WGS84 Polygon/MultiPolygon. Valid source holes are protected.</p>
    <label>Policy JSON<textarea rows={12} value={policy} onChange={e => setPolicy(e.target.value)} /></label><button className="button button-secondary" disabled={!enabled}>Save new policy version</button></form></details>
}

function ParcelMap({ features, selected, onSelect }: { features: w.Feature[]; selected?: w.Evidence; onSelect: (id: string) => void }) {
  const host = useRef<HTMLDivElement>(null)
  const map = useRef<maplibregl.Map | null>(null)
  const [readyMap, setReadyMap] = useState<maplibregl.Map | null>(null)
  const ready = readyMap !== null && readyMap === map.current
  const [basemap, setBasemap] = useState(true)
  const [basemapLoaded, setBasemapLoaded] = useState(false)
  const [basemapError, setBasemapError] = useState('')
  const [mapError, setMapError] = useState('')
  const [retry, setRetry] = useState(0)
  const [visible, setVisible] = useState<Record<string, boolean>>({})
  const basemapEnabled = useRef(basemap)
  basemapEnabled.current = basemap
  const select = useRef(onSelect)
  select.current = onSelect
  useEffect(() => {
    if (!host.current) return
    const container = host.current
    let instance: maplibregl.Map | undefined
    let observer: ResizeObserver | undefined
    setReadyMap(null); setMapError(''); setBasemapError(''); setBasemapLoaded(false)
    try {
      instance = new maplibregl.Map({ container, center: [73, 20], zoom: 5,
        style: { version: 8, sources: {}, layers: [{ id: 'background', type: 'background', paint: { 'background-color': '#e8efe9' } }] } })
      map.current = instance
      instance.addControl(new maplibregl.NavigationControl())
      instance.addControl(new maplibregl.ScaleControl({ unit: 'metric' }))
      // Parcel overlays can initialize as soon as the style is ready, even if
      // the external basemap is slow or unavailable.
      instance.on('style.load', () => { if (map.current === instance) setReadyMap(instance || null) })
      instance.on('sourcedata', event => {
        if (event.sourceId === 'basemap' && event.tile?.state === 'loaded') setBasemapLoaded(true)
      })
      instance.on('error', event => {
        if (('sourceId' in event && event.sourceId === 'basemap') || event.error.message.includes('basemaps.cartocdn.com')) {
          if (basemapEnabled.current) setBasemapError('Basemap tiles could not load. Check your connection or turn off the basemap; parcel overlays remain available.')
        } else setMapError(`Map could not render: ${event.error.message}`)
      })
      instance.on('webglcontextlost', () => { setReadyMap(null); setMapError('The browser lost its map graphics context. Retry the map or enable hardware acceleration.') })
      instance.on('webglcontextrestored', () => { setMapError(''); setReadyMap(instance || null); instance?.resize() })
      observer = new ResizeObserver(() => instance?.resize())
      observer.observe(container)
    } catch {
      instance?.remove()
      map.current = null
      container.replaceChildren()
      setMapError('Map could not start. Enable WebGL/hardware acceleration in your browser, then retry.')
    }
    return () => {
      observer?.disconnect()
      if (map.current === instance) { instance?.remove(); map.current = null }
    }
  }, [retry])
  useEffect(() => {
    const instance = readyMap
    if (!instance || instance !== map.current) return
    const data: GeoJSON.FeatureCollection = { type: 'FeatureCollection', features: features.filter(f => f.geometry && visible[f.dataset_id || ''] !== false).map(f => ({
      type: 'Feature', geometry: f.geometry!, properties: { feature_id: f.id, status: f.status,
        comparison: f.id === selected?.left_feature_id ? 'before' : f.id === selected?.right_feature_id ? 'after' : '' } })) }
    if (selected?.before_geometry) data.features.push({ type: 'Feature', geometry: selected.before_geometry, properties: { comparison: 'before' } })
    if (selected?.after_geometry) data.features.push({ type: 'Feature', geometry: selected.after_geometry, properties: { comparison: 'after' } })
    const source = instance.getSource('parcels') as maplibregl.GeoJSONSource | undefined
    if (source) source.setData(data)
    else {
      instance.addSource('parcels', { type: 'geojson', data })
      instance.addLayer({ id: 'parcel-fill', type: 'fill', source: 'parcels', paint: {
        'fill-color': ['match', ['get', 'comparison'], 'before', '#b87a1a', 'after', '#3c7488', '#4f9981'], 'fill-opacity': .32 } })
      instance.addLayer({ id: 'parcel-line', type: 'line', source: 'parcels', paint: { 'line-color': '#145a49', 'line-width': 2 } })
      instance.on('click', 'parcel-fill', event => { const id = event.features?.[0]?.properties?.feature_id; if (id) select.current(String(id)) })
    }
    const focusIds = [selected?.left_feature_id, selected?.right_feature_id, selected?.source_feature_id, selected?.comparison_feature_id]
    const focus = selected ? data.features.filter(f => focusIds.includes(f.properties?.feature_id) || f.properties?.comparison) : data.features
    const bounds = new maplibregl.LngLatBounds()
    const extend = (coordinates: unknown): void => {
      if (!Array.isArray(coordinates)) return
      if (typeof coordinates[0] === 'number' && typeof coordinates[1] === 'number') bounds.extend([coordinates[0], coordinates[1]])
      else coordinates.forEach(extend)
    }
    focus.forEach(feature => { if ('coordinates' in feature.geometry) extend(feature.geometry.coordinates) })
    if (!bounds.isEmpty()) instance.fitBounds(bounds, { padding: 50, maxZoom: 17, duration: 0 })
  }, [features, selected, readyMap, visible])
  useEffect(() => {
    const instance = readyMap
    if (!instance || instance !== map.current) return
    if (basemap && !instance.getSource('basemap')) {
      instance.addSource('basemap', { type: 'raster', tiles: ['https://basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png'],
        tileSize: 256, maxzoom: 20, attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors · © <a href="https://carto.com/attributions">CARTO</a>' })
      instance.addLayer({ id: 'basemap', type: 'raster', source: 'basemap' }, instance.getLayer('parcel-fill') ? 'parcel-fill' : undefined)
    }
    if (instance.getLayer('basemap')) instance.setLayoutProperty('basemap', 'visibility', basemap ? 'visible' : 'none')
    if (!basemap) setBasemapError('')
  }, [basemap, readyMap])
  const spatialCount = features.filter(f => f.geometry).length
  const visibleCount = features.filter(f => f.geometry && visible[f.dataset_id || ''] !== false).length
  return <section className="panel live-map" data-map-ready={ready}><div className="section-head"><strong>{spatialCount} spatial source records</strong>
    <label><input type="checkbox" checked={basemap} onChange={e => setBasemap(e.target.checked)} /> Show basemap</label></div>
    <div className="map-legend-static">{[...new Map(features.map(f => [f.dataset_id || '', f.dataset_name || f.dataset_id])).entries()].map(([id, name]) => <label key={id}><input type="checkbox" checked={visible[id] !== false} onChange={e => setVisible({ ...visible, [id]: e.target.checked })} /> {name} </label>)}</div>
    {(mapError || basemapError) && <div className="map-notice" role={mapError ? 'alert' : 'status'}><span>{mapError || basemapError}</span>
      <button className="button button-secondary button-small" onClick={() => setRetry(value => value + 1)}>Retry map</button></div>}
    {!spatialCount && <p className="map-empty">No normalized parcel geometry yet. Upload spatial data and confirm its CRS in Datasets.</p>}
    {spatialCount > 0 && !visibleCount && <p className="map-empty">All source layers are hidden. Turn on a source layer to display its parcels.</p>}
    <div ref={host} className="parcel-map-canvas" role="region" aria-label="Parcel map" />
    {!mapError && <p className="map-status" role="status">{!ready ? 'Loading map…' : !basemap ? 'Basemap off · parcel overlays available' : basemapError ? 'Parcel overlays available' : basemapLoaded ? 'Basemap loaded' : 'Loading basemap…'}</p>}
    <div className="map-legend-static">Green: normalized source · Amber: baseline/before · Blue: comparison/after</div></section>
}

function DatasetInspector({ project, dataset, writable, reviewable, refresh, run }: { project: string; dataset: w.Dataset; writable: boolean; reviewable: boolean; refresh: () => Promise<void>; run: (action: () => Promise<void>) => void }) {
  const [features, setFeatures] = useState<w.Feature[]>([])
  const [mapping, setMapping] = useState<w.Mapping>()
  const [crs, setCrs] = useState(dataset.declared_crs || '')
  const [reason, setReason] = useState('')
  const [error, setError] = useState('')
  const [templates, setTemplates] = useState<{ id: string; dataset_id: string; version: number; mapping: Record<string, string> }[]>([])
  const [baselineReason, setBaselineReason] = useState('')
  const path = `${w.projectPath(project)}/datasets/${dataset.id}`
  useEffect(() => { void Promise.all([w.get<w.Feature[]>(`${path}/features`), w.get<w.Mapping>(`${path}/mapping`)])
    .then(([items, item]) => { setFeatures(items); setMapping(item) }).catch(e => setError(String(e))) }, [path, dataset])
  useEffect(() => { void w.get<typeof templates>(`${w.projectPath(project)}/mapping-templates`).then(setTemplates).catch(e => setError(String(e))) }, [project, dataset])
  return <section className="panel inspector"><h2>{dataset.name}</h2>{error && <p role="alert">{error}</p>}
    <dl><dt>Source / capture date</dt><dd>{dataset.source_organization || 'Not supplied'} · {dataset.capture_date || 'Not supplied'}</dd>
      <dt>SHA-256</dt><dd className="hash">{dataset.content_hash}</dd><dt>Source → display → analysis CRS</dt><dd>{dataset.declared_crs || 'Unknown'} → {dataset.normalized_crs || 'Blocked'} → {dataset.analysis_crs || 'Unavailable'}</dd></dl>
    <form onSubmit={e => { e.preventDefault(); run(async () => { await w.post(`${path}/confirm-crs`, { crs, reason }); await refresh() }) }}>
      <h3>Confirm or correct CRS</h3><label>Source CRS<input required value={crs} placeholder="Documented EPSG code or WKT" onChange={e => setCrs(e.target.value)} /></label>
      <label>CRS rationale<input required value={reason} onChange={e => setReason(e.target.value)} /></label>
      <button className="button button-secondary" disabled={!writable}>Confirm CRS and reprocess</button></form>
    <form onSubmit={e => { e.preventDefault(); if (mapping) run(async () => { await w.post(`${path}/mapping`, { mapping: mapping.mapping, confirm: true }); await refresh() }) }}>
      <h3>Schema mapping · {mapping?.status || 'Loading'} v{mapping?.version || 0}</h3>
      <p>Confirm identifier semantics and administrative context. Original strings, leading zeros, area values and units are preserved.</p>
      <label>Reuse confirmed template<select defaultValue="" onChange={e => { const template = templates.find(t => t.id === e.target.value); if (template) setMapping(current => current ? { ...current, mapping: template.mapping } : current) }}><option value="">Select a saved mapping</option>{templates.map(t => <option key={t.id} value={t.id}>{t.dataset_id.slice(0,8)} · v{t.version}</option>)}</select></label>
      <div className="mapping-grid">{canonicalFields.map(field => <label key={field}>{field}<select aria-label={`Map ${field}`} value={mapping?.mapping[field] || ''} onChange={e => setMapping(current => {
        if (!current) return current; const next = { ...current.mapping }; if (e.target.value) next[field] = e.target.value; else delete next[field]; return { ...current, mapping: next }
      })}><option value="">Not mapped</option>{mapping?.source_fields.map(source => <option key={source.name}>{source.name}</option>)}</select></label>)}</div>
      <button className="button button-primary" disabled={!writable || !mapping}>Confirm mapping</button></form>
    <details><summary>Quality report</summary><Json value={dataset.validation_report} /></details>
    <details open><summary>Record preview · {features.length} records (including quarantine)</summary><label>Baseline rationale<input value={baselineReason} onChange={e => setBaselineReason(e.target.value)} /></label><div className="record-preview">
      {features.slice(0, 100).map(feature => <details key={feature.id}><summary>{label(feature)} · {feature.status} {feature.processing_reason}</summary>
        <Json value={{ attributes: feature.attributes, original_geometry: feature.original_geometry, normalized_geometry: feature.geometry }} />
        <button className="button button-secondary" disabled={!reviewable || feature.status !== 'processed' || !baselineReason.trim()} onClick={() => run(async () => {
          await w.post(`${w.projectPath(project)}/features/${feature.id}/baseline`, { geometry_source_id: feature.geometry ? feature.id : null, attribute_source_id: feature.id, rationale: baselineReason }); await refresh()
        })}>Approve standalone baseline</button></details>)}
      {features.length > 100 && <p>First 100 shown; all records retained by the API.</p>}</div></details>
  </section>
}

function Baseline({ parcel, features, project, canReview, refresh, run }: { parcel: w.Parcel; features: w.Feature[]; project: string; canReview: boolean; refresh: () => Promise<void>; run: (action: () => Promise<void>) => void }) {
  const members = features.filter(f => parcel.source_feature_ids.includes(f.id))
  const [geometry, setGeometry] = useState(parcel.selection?.geometry_source_id || '')
  const [attributes, setAttributes] = useState(parcel.selection?.attribute_source_id || '')
  const [attributeSources, setAttributeSources] = useState<Record<string,string>>(parcel.selection?.attribute_sources || {})
  const [rationale, setRationale] = useState('')
  return <form className="panel inspector" onSubmit={e => { e.preventDefault(); run(async () => {
    await w.post(`${w.projectPath(project)}/parcels/${parcel.id}/selection`, { geometry_source_id: geometry || null, attribute_source_id: attributes,
      expected_revision: parcel.selection?.revision || 0, attribute_sources: attributeSources, rationale }); await refresh()
  }) }}><h3>Canonical parcel {parcel.id.slice(0, 8)}</h3><p>{parcel.source_feature_ids.length} linked records. Identity acceptance does not select a boundary.</p>
    <label>Geometry source<select aria-label="Geometry source" value={geometry} onChange={e => setGeometry(e.target.value)}><option value="">Attribute-only publication</option>{members.filter(f => f.geometry).map(f => <option key={f.id} value={f.id}>{label(f)} · {f.dataset_id?.slice(0,8)}</option>)}</select></label>
    <label>Attribute source<select required aria-label="Attribute source" value={attributes} onChange={e => setAttributes(e.target.value)}><option value="">Select source</option>{members.map(f => <option key={f.id} value={f.id}>{label(f)} · {f.dataset_id?.slice(0,8)}</option>)}</select></label>
    <details><summary>Per-field source precedence</summary><p>Choose overrides after comparing authority, capture date, accuracy and purpose. There is no universal source ranking.</p>
      {[...new Set(members.flatMap(f => [...Object.keys(f.attributes), ...Object.keys(f.canonical_attributes || {})]))].map(field => <label key={field}>{field}<select value={attributeSources[field] || ''} onChange={e => {
        const next = { ...attributeSources }; if (e.target.value) next[field] = e.target.value; else delete next[field]; setAttributeSources(next)
      }}><option value="">Use selected attribute source</option>{members.filter(f => field in f.attributes || field in (f.canonical_attributes || {})).map(f => <option key={f.id} value={f.id}>{label(f)} · {f.dataset_id?.slice(0,8)} · {String(f.canonical_attributes?.[field] ?? f.attributes[field])}</option>)}</select></label>)}</details>
    <label>Selection rationale<input required value={rationale} onChange={e => setRationale(e.target.value)} /></label>
    <button className="button button-primary" disabled={!canReview}>Approve baseline selection</button><span> Revision {parcel.selection?.revision || 0}</span>
  </form>
}

export default function App() {
  const [user, setUser] = useState<w.User>()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [projects, setProjects] = useState<w.Project[]>([])
  const [project, setProject] = useState('')
  const [datasets, setDatasets] = useState<w.Dataset[]>([])
  const [features, setFeatures] = useState<w.Feature[]>([])
  const [evidence, setEvidence] = useState<w.Evidence[]>([])
  const [parcels, setParcels] = useState<w.Parcel[]>([])
  const [versions, setVersions] = useState<w.Version[]>([])
  const [view, setView] = useState<View>('overview')
  const [selected, setSelected] = useState('')
  const [datasetId, setDatasetId] = useState('')
  const [pair, setPair] = useState({ before: '', after: '' })
  const [job, setJob] = useState<w.Job>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [search, setSearch] = useState('')
  const [note, setNote] = useState('')
  const [validation, setValidation] = useState<Record<string, unknown>>()
  const [outputCrs, setOutputCrs] = useState('EPSG:4326')
  const [newProject, setNewProject] = useState('')
  const [file, setFile] = useState<File>()
  const [capture, setCapture] = useState('')
  const [source, setSource] = useState('')
  const [member, setMember] = useState('')
  const [memberRole, setMemberRole] = useState('viewer')
  const activeProject = useRef(project)
  activeProject.current = project
  const writable = user?.role !== 'viewer'
  const canReview = user?.role === 'reviewer' || user?.role === 'admin'
  const path = w.projectPath(project)
  const refresh = useCallback(async () => {
    if (!project) return
    const [ds, matches, changes, conflicts, entities, publications] = await Promise.all([
      w.get<w.Dataset[]>(`${w.projectPath(project)}/datasets`), w.get<w.Evidence[]>(`${w.projectPath(project)}/matches`),
      w.get<w.Evidence[]>(`${w.projectPath(project)}/changes`), w.get<w.Evidence[]>(`${w.projectPath(project)}/conflicts`),
      w.get<w.Parcel[]>(`${w.projectPath(project)}/parcels`), w.get<w.Version[]>(`${w.projectPath(project)}/versions`)])
    if (activeProject.current !== project) return
    setDatasets(ds); setParcels(entities); setVersions(publications)
    setEvidence([...matches.map(e => ({ ...e, target: 'match' as const })), ...changes.map(e => ({ ...e, target: 'change' as const })), ...conflicts.map(e => ({ ...e, target: 'conflict' as const }))])
    const loaded = (await Promise.all(ds.map(async d => (await w.get<w.Feature[]>(`${w.projectPath(project)}/datasets/${d.id}/features`)).map(f => ({ ...f, dataset_id: d.id, dataset_name: d.name }))))).flat()
    if (activeProject.current === project) setFeatures(loaded)
  }, [project])
  const run = useCallback((action: () => Promise<void>) => { setBusy(true); setError(''); setNotice(''); void action().catch(e => setError(e instanceof Error ? e.message : String(e))).finally(() => setBusy(false)) }, [])
  useEffect(() => { if (project) { setSelected(''); setDatasetId(''); setValidation(undefined); setPair({ before: '', after: '' }); run(refresh) } }, [project, refresh, run])
  const login = async () => { const result = await w.post<{ access_token: string; user: w.User }>('/auth/token', { username, password }); localStorage.setItem('geosyncai_token', result.access_token); setUser(result.user); const ps = await w.get<w.Project[]>('/projects'); setProjects(ps); setProject(ps[0]?.id || '') }
  const logout = () => { localStorage.removeItem('geosyncai_token'); setUser(undefined); setProject(''); setDatasets([]); setFeatures([]); setEvidence([]); setNotice(''); setError('') }
  const selectedEvidence = evidence.find(e => e.id === selected)
  const selectedDataset = datasets.find(d => d.id === datasetId)
  const active = projects.find(p => p.id === project)
  const open = evidence.filter(e => !['accepted', 'rejected', 'validated', 'resolved'].includes(e.status))
  const download = (kind: api.ExportKind) => run(async () => { await api.downloadExport(project, kind, undefined, outputCrs); setNotice(`${kind} downloaded`) })
  const process = (type: string) => run(async () => {
    if (!pair.before || !pair.after || pair.before === pair.after) throw new Error('Choose two different source datasets')
    if (type === 'change_detection') {
      const before = datasets.find(d => d.id === pair.before), after = datasets.find(d => d.id === pair.after)
      if (!before?.capture_date || !after?.capture_date || before.capture_date >= after.capture_date) throw new Error('Choose a baseline captured before the comparison snapshot')
    }
    await w.runJob(project, type, type === 'match' ? { left_dataset_id: pair.before, right_dataset_id: pair.after }
      : { before_dataset_id: pair.before, after_dataset_id: pair.after }, setJob)
    await refresh(); setNotice('Processing completed')
  })
  const decide = (decision: string) => run(async () => {
    if (!selectedEvidence || !note.trim()) throw new Error('Enter a review rationale')
    try { await w.post(`${path}/reviews/${selectedEvidence.target}/${selectedEvidence.id}`, { decision, rationale: note, expected_revision: selectedEvidence.revision }) }
    finally { await refresh() }
    setNote(''); setValidation(undefined); setNotice('Review decision saved')
  })
  if (!user) return <main className="login-page"><div className="login-art"><Brand /><div className="art-copy"><p className="eyebrow">LAND DATA HARMONIZATION</p><h1>Align the data.<br /><em>Explain the match.</em></h1><p>Reviewable evidence behind every parcel decision.</p></div><p>GeoSyncAI · SIH26013 · Git Good</p></div>
    <div className="login-panel"><form className="login-form" onSubmit={e => { e.preventDefault(); run(login) }}><Brand /><h2>Officer workspace</h2><label>Username<input required value={username} onChange={e => setUsername(e.target.value)} autoComplete="username" /></label>
      <label>Password<input required type="password" value={password} onChange={e => setPassword(e.target.value)} autoComplete="current-password" /></label>{error && <div role="alert" className="error-banner">{error}</div>}
      <button className="button button-primary button-wide" disabled={busy}>{busy ? 'Connecting…' : 'Sign in to workspace'}</button><p>Local demonstration accounts must be explicitly seeded. All workspace actions use the API.</p></form></div></main>
  return <div className="app-shell"><aside className="sidebar"><div className="sidebar-brand"><Brand /></div><div className="workspace-switcher"><label>Project<select aria-label="Project" value={project} onChange={e => setProject(e.target.value)}><option value="">Select project</option>{projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label></div>
    <nav className="sidebar-nav">{(Object.keys(titles) as View[]).map(v => <button key={v} className={view === v ? 'active' : ''} onClick={() => setView(v)}>{titles[v]}</button>)}</nav>
    <div className="sidebar-bottom"><p>{user.username} · {user.role}</p><button className="button button-secondary" onClick={logout}>Sign out</button></div></aside>
    <div className="app-content"><header className="app-header"><strong>{active?.name || 'Select or create project'} / {titles[view]}</strong><div className="header-actions"><button className="button button-secondary" disabled={!project || busy} onClick={() => run(refresh)}>Refresh</button>
      <select className="mobile-project" aria-label="Mobile project" value={project} onChange={e => setProject(e.target.value)}><option value="">Select project</option>{projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select>
      <button className="button button-secondary mobile-project" onClick={logout}>Sign out</button>
      <select aria-label="Export" defaultValue="" disabled={!versions.length} onChange={e => { if (e.target.value) download(e.target.value as api.ExportKind); e.target.value = '' }}><option value="">Export latest version</option>{['geojson', 'gpkg', 'csv', 'lineage', 'quality'].map(kind => <option key={kind}>{kind}</option>)}</select></div></header>
      <main className="page-content" aria-busy={busy}>{error && <div role="alert" className="error-banner">{error} <button onClick={() => run(refresh)}>Refresh evidence</button></div>}{notice && <p role="status" className="notice-banner">{notice}</p>}{busy && <p role="status">Working…</p>}
      {view === 'overview' && <><div className="welcome-row"><div><p className="eyebrow">{new Date().toLocaleDateString()}</p><h1>Welcome, {user.username}</h1><p>Source records, decisions and publication evidence.</p></div></div>
        <div className="metric-grid">{[['Sources', datasets.length], ['Records', features.length], ['Open review', open.length], ['Published versions', versions.length]].map(([name, value]) => <section key={name} className="metric-card"><div className="metric-value">{value}</div><div className="metric-label">{name}</div></section>)}</div>
        <form className="panel inspector" onSubmit={e => { e.preventDefault(); run(async () => { const p = await w.post<w.Project>('/projects', { name: newProject }); setProjects(await w.get<w.Project[]>('/projects')); setProject(p.id); setNewProject('') }) }}><h3>Create study-area project</h3><label>Project name<input required value={newProject} onChange={e => setNewProject(e.target.value)} /></label><button className="button button-primary" disabled={!writable || busy}>Create project</button></form>
        {project && <><section className="panel inspector"><h3>Processing policy</h3><p>Only explicit reviewer-approved baseline selections publish. Pending changes affecting selected parcels block publication. Rejected changes retain the approved baseline. Unselected records are reported as exclusions.</p>
          <PolicyEditor project={project} enabled={canReview} run={run} />
          <button className="button button-secondary" disabled={!writable || busy} onClick={() => run(async () => { await w.post(`${path}/bootstrap-synthetic?count=25`); await refresh(); setNotice('Explicit synthetic pair generated') })}>Generate labelled synthetic sources</button></section>
          {user.role === 'admin' && <form className="panel inspector" onSubmit={e => { e.preventDefault(); run(async () => { await w.post(`${path}/members`, { username: member, project_role: memberRole }); setNotice('Membership saved') }) }}><h3>Project membership</h3><label>Member username<input required value={member} onChange={e => setMember(e.target.value)} /></label><label>Project role<select value={memberRole} onChange={e => setMemberRole(e.target.value)}>{['viewer','processor','reviewer'].map(role => <option key={role}>{role}</option>)}</select></label><button className="button button-primary">Save membership</button></form>}</>}
      </>}
      {view === 'datasets' && project && <><h1>Dataset registry</h1><form className="panel inspector" onSubmit={e => { e.preventDefault(); run(async () => { if (!file) throw new Error('Choose a source file'); const d = await w.upload(project, file, capture, source); await refresh(); setDatasetId(d.id); setNotice('Upload preserved and inspected') }) }}>
        <label>Dataset file<input type="file" accept=".geojson,.json,.csv,.gpkg,.zip" required onChange={e => setFile(e.target.files?.[0])} /></label><label>Capture date<input type="date" value={capture} onChange={e => setCapture(e.target.value)} /></label><label>Source organization<input value={source} onChange={e => setSource(e.target.value)} /></label><button className="button button-primary" disabled={!writable || busy}>Upload dataset</button></form>
        <section className="panel inspector"><h3>Source pair</h3>{(['before','after'] as const).map(side => <label key={side}>{side === 'before' ? 'Baseline' : 'Comparison'}<select aria-label={side === 'before' ? 'Baseline' : 'Comparison'} value={pair[side]} onChange={e => setPair({ ...pair, [side]: e.target.value })}><option value="">Choose dataset</option>{datasets.map(d => <option key={d.id} value={d.id}>{d.name} · {d.capture_date || 'Undated'}</option>)}</select></label>)}
          <button className="button button-primary" disabled={!writable || busy} onClick={() => process('match')}>Generate matches</button> <button className="button button-secondary" disabled={!writable || busy} onClick={() => process('change_detection')}>Compare dated snapshots</button>
          {job && <div role="status">{job.job_type}: {job.status} {job.error} {job.id}</div>}</section>
        <section className="panel inspector">{datasets.map(d => <div className="source-row" key={d.id}><button className="text-button" onClick={() => setDatasetId(d.id)}>{d.name}</button><span>{d.record_count} records · {d.status} · {d.declared_crs || 'Unknown CRS'}</span><button className="button button-secondary" disabled={!writable || busy} onClick={() => run(async () => { await w.runJob(project, 'topology', { dataset_id: d.id }, setJob); await refresh() })}>Check topology</button></div>)}</section>
        {selectedDataset && <DatasetInspector key={selectedDataset.id} project={project} dataset={selectedDataset} writable={writable} reviewable={canReview} refresh={refresh} run={run} />}</>}
      {(view === 'review' || view === 'alerts') && project && <><h1>{titles[view]}</h1><label>Search evidence<input value={search} onChange={e => setSearch(e.target.value)} /></label><div className="evidence-workspace"><section className="panel inspector evidence-list">
        {evidence.filter(e => (view !== 'alerts' || e.target === 'change') && format(e).toLowerCase().includes(search.toLowerCase())).map(e => <button key={e.id} className={`proposal-list-row ${selected === e.id ? 'selected' : ''}`} onClick={() => { setSelected(e.id); setNote('') }}><span>{e.target} · {e.change_type || label(features.find(f => f.id === e.left_feature_id))}</span><span>{e.status}</span></button>)}
        {!evidence.length && <p>No processing evidence yet.</p>}</section><section><ParcelMap features={features} selected={selectedEvidence} onSelect={id => { const e = evidence.find(e => [e.left_feature_id,e.right_feature_id,e.source_feature_id,e.comparison_feature_id].includes(id)); if (e) setSelected(e.id) }} />
        {selectedEvidence && <section className="panel inspector"><h2>Parcel evidence card</h2><p>{selectedEvidence.target} · {selectedEvidence.status} · revision {selectedEvidence.revision}</p>
          {selectedEvidence.target === 'match' && <p>Uncalibrated ranking score: {selectedEvidence.score?.toFixed(3)}. Not a probability of correctness.</p>}
          <Json value={selectedEvidence.evidence || selectedEvidence.details || selectedEvidence.description} />
          <details><summary>Before / after source records</summary>{features.filter(f => [selectedEvidence.left_feature_id,selectedEvidence.right_feature_id,selectedEvidence.source_feature_id,selectedEvidence.comparison_feature_id].includes(f.id)).map(f => <Json key={f.id} value={f} />)}</details>
          <label>Review rationale<textarea value={note} onChange={e => setNote(e.target.value)} /></label><div className="title-actions">{['under_review','accepted','rejected','needs_field_verification'].map(decision => <button key={decision} className="button button-secondary" disabled={!canReview || busy || !note.trim() || ['accepted','validated','rejected','resolved'].includes(selectedEvidence.status)} onClick={() => decide(decision)}>{decision.replaceAll('_', ' ')}</button>)}</div>
          {!canReview && <p>Reviewer permission is required for decisions.</p>}</section>}</section></div>
        <h2>Approved baseline selections</h2>{parcels.map(p => <Baseline key={`${p.id}-${p.selection?.revision || 0}`} parcel={p} features={features} project={project} canReview={canReview} refresh={refresh} run={run} />)}
      </>}
      {view === 'versions' && project && <><h1>Versions & history</h1><div className="title-actions"><button className="button button-secondary" disabled={!writable || busy} onClick={() => run(async () => { setValidation(await w.post(`${path}/validate`)); setNotice('Validation completed; inspect failures and exclusions') })}>Run validation</button>
        <button className="button button-primary" disabled={!canReview || busy} onClick={() => run(async () => { await w.post(`${path}/publish`); await refresh(); setNotice('Immutable version published') })}>Publish version</button></div>
        <label>GeoPackage output CRS<input value={outputCrs} onChange={e => setOutputCrs(e.target.value)} /></label><p>GeoJSON exports always use longitude/latitude EPSG:4326.</p>
        {validation && <section className="panel inspector"><h2>Validation: {validation.valid ? 'passed' : 'blocked'}</h2><Json value={validation} /></section>}
        {versions.map(v => <section className="panel inspector" key={v.id}><h2>Version {v.version}</h2><p>{new Date(v.created_at).toLocaleString()}</p><details><summary>View lineage</summary><Json value={v.lineage_manifest} /></details>
          <button className="button button-secondary" onClick={() => run(async () => { await api.downloadExport(project, 'lineage', v.id) })}>Download this version's lineage</button>
          <button className="button button-secondary" disabled={!canReview || busy} onClick={() => run(async () => { await w.post(`${path}/versions/${v.id}/rollback`); await refresh(); setNotice('Traceable rollback version created') })}>Restore as new version</button></section>)}</>}
      {!project && view !== 'overview' && <p>Create or select a project in Overview.</p>}
      </main><footer className="app-footer">GeoSyncAI · SIH26013 · Team Git Good / 151551 · Review support, not legal certification</footer></div></div>
}
