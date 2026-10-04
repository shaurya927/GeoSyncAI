import json

from shapely.geometry import box, mapping

from test_backend import _project_with_reviewer, _upload, _geojson, headers


def prepare_pair(client, auth_token, name="selected-pair", shift=0.0):
    project, admin, reviewer = _project_with_reviewer(client, auth_token, name)
    left = _upload(client, project, admin, "left.geojson", _geojson("001", 73, 20, "village"), capture_date='2024-01-01')
    right = _upload(client, project, admin, "right.geojson", _geojson("001", 73 + shift, 20, "village"), capture_date='2025-01-01')
    for dataset in (left, right):
        response = client.post(f"/api/projects/{project}/datasets/{dataset['id']}/mapping", headers=headers(admin),
                               json={"mapping": {"parcel_id": "parcel_id", "village": "village"}, "confirm": True})
        assert response.status_code == 200, response.text
    matched = client.post(f"/api/projects/{project}/match", headers=headers(admin), json={
        "left_dataset_id": left['id'], "right_dataset_id": right['id']})
    assert matched.status_code == 200, matched.text
    proposal = matched.json()["proposals"][0]
    accepted = client.post(f"/api/projects/{project}/reviews/match/{proposal['id']}", headers=headers(reviewer),
                           json={"decision": "accepted", "rationale": "Reviewed identity", "expected_revision": 1})
    assert accepted.status_code == 200, accepted.text
    parcel = client.get(f"/api/projects/{project}/parcels", headers=headers(reviewer)).json()[0]
    selection = client.post(f"/api/projects/{project}/parcels/{parcel['id']}/selection", headers=headers(reviewer),
                           json={"attribute_source_id": proposal['left_feature_id'], "geometry_source_id": proposal['left_feature_id'],
                                 "rationale": "Retain baseline geometry and attributes"})
    assert selection.status_code == 200, selection.text
    return project, admin, reviewer, left, right, proposal, parcel


def test_rejected_boundary_retains_baseline_and_stable_identity(client, auth_token):
    project, admin, reviewer, left, right, proposal, parcel = prepare_pair(client, auth_token, "rejected-boundary", .00005)
    original = client.get(f"/api/projects/{project}/datasets/{left['id']}/features", headers=headers(admin)).json()
    detected = client.post(f"/api/projects/{project}/changes/detect", headers=headers(admin), json={
        "before_dataset_id": left['id'], "after_dataset_id": right['id'], "geometry_tolerance": .5})
    assert detected.status_code == 200, detected.text
    changes = client.get(f"/api/projects/{project}/changes", headers=headers(admin)).json()
    assert len(changes) == 1
    assert not client.post(f"/api/projects/{project}/validate", headers=headers(reviewer)).json()['valid']
    deferred = client.post(f"/api/projects/{project}/reviews/change/{changes[0]['id']}", headers=headers(reviewer),
                           json={"decision": "needs_field_verification", "rationale": "Need further evidence", "expected_revision": 1})
    assert deferred.status_code == 200
    assert not client.post(f"/api/projects/{project}/validate", headers=headers(reviewer)).json()['valid']
    rejected = client.post(f"/api/projects/{project}/reviews/change/{changes[0]['id']}", headers=headers(reviewer),
                           json={"decision": "rejected", "rationale": "Retain baseline", "expected_revision": 2})
    assert rejected.status_code == 200, rejected.text
    report = client.post(f"/api/projects/{project}/validate", headers=headers(reviewer)).json()
    assert report['valid'], report
    for _ in range(2):
        version = client.post(f"/api/projects/{project}/publish", headers=headers(reviewer))
        assert version.status_code == 200, version.text
        exported = client.get(f"/api/projects/{project}/versions/{version.json()['id']}/export", headers=headers(reviewer)).json()
        assert len(exported['features']) == 1
        assert exported['features'][0]['id'] == parcel['id']
        assert exported['features'][0]['geometry'] == original[0]['geometry']
    assert client.get(f"/api/projects/{project}/datasets/{left['id']}/features", headers=headers(admin)).json() == original


def test_validation_stales_after_selection_and_exports_reopen(client, auth_token):
    import csv
    import io
    from fiona.io import MemoryFile
    from pyproj import CRS
    project, admin, reviewer, left, right, proposal, parcel = prepare_pair(client, auth_token, "exports")
    assert client.post(f"/api/projects/{project}/validate", headers=headers(reviewer)).json()['valid']
    changed = client.post(f"/api/projects/{project}/parcels/{parcel['id']}/selection", headers=headers(reviewer), json={
        "attribute_source_id": proposal['right_feature_id'], "geometry_source_id": proposal['left_feature_id'],
        "rationale": "Explicit alternate attribute source", "expected_revision": 1})
    assert changed.status_code == 200
    assert client.post(f"/api/projects/{project}/publish", headers=headers(reviewer)).status_code == 409
    assert client.post(f"/api/projects/{project}/validate", headers=headers(reviewer)).json()['valid']
    published = client.post(f"/api/projects/{project}/publish", headers=headers(reviewer)).json()
    url = f"/api/projects/{project}/versions/{published['id']}/export"
    csv_export = client.get(url + '?format=csv', headers=headers(reviewer))
    rows = list(csv.DictReader(io.StringIO(csv_export.text)))
    assert len(rows) == 2 and {r['parcel_entity_id'] for r in rows} == {parcel['id']}
    gpkg = client.get(url + '?format=gpkg&output_crs=EPSG:32643', headers=headers(reviewer))
    assert gpkg.status_code == 200, gpkg.text
    with MemoryFile(gpkg.content, ext='.gpkg') as memory:
        with memory.open() as collection:
            assert CRS(collection.crs) == CRS(32643)
            assert len(collection) == 1
            feature = next(iter(collection))
            assert len(json.loads(feature['properties']['lineage'])['sources']) == 2
            assert feature['geometry']['coordinates'][0][0][0] > 100000
    rollback = client.post(f"/api/projects/{project}/versions/{published['id']}/rollback", headers=headers(reviewer))
    assert rollback.status_code == 200, rollback.text
    assert rollback.json()['base_version_id'] == published['id'] and rollback.json()['version'] == 2
    restored = client.get(f"/api/projects/{project}/versions/{rollback.json()['id']}/export", headers=headers(reviewer)).json()
    original = client.get(url, headers=headers(reviewer)).json()
    assert restored['features'][0]['geometry'] == original['features'][0]['geometry']
    assert original['features'][0]['properties']['_lineage'].get('rollback_of_version_id') is None


def test_stage_failure_rolls_back_and_replay_has_no_duplicate_effects(client, auth_token, monkeypatch):
    from app import tasks
    from app.db import SessionLocal
    from app.models import Job, MatchProposal
    from sqlalchemy import select, func
    project, admin, _, left, right, _, _ = prepare_pair(client, auth_token, "job-rollback")
    original = tasks.run_matching
    def fail_after_writes(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('simulated interruption before completion')
    monkeypatch.setattr(tasks, 'run_matching', fail_after_writes)
    response = client.post(f"/api/projects/{project}/jobs?job_type=match", headers=headers(admin), json={
        'left_dataset_id': left['id'], 'right_dataset_id': right['id']})
    job_id = response.json()['id']
    with SessionLocal() as db:
        assert db.get(Job, job_id).status == 'failed'
        assert db.scalar(select(func.count()).select_from(MatchProposal).where(MatchProposal.project_id == project)) == 1
    monkeypatch.setattr(tasks, 'run_matching', original)
    tasks.execute_job(job_id)
    tasks.execute_job(job_id)
    with SessionLocal() as db:
        assert db.get(Job, job_id).status == 'succeeded'
        assert db.scalar(select(func.count()).select_from(MatchProposal).where(MatchProposal.project_id == project)) == 2


def test_resulting_neighborhood_overlap_blocks_publication(client, auth_token):
    project, admin, reviewer = _project_with_reviewer(client, auth_token, 'neighborhood')
    body = json.dumps({'type':'FeatureCollection', 'features':[
        {'type':'Feature','id':str(i),'properties':{'parcel_id':str(i)},'geometry':mapping(box(73+i*.0005,20,73.001+i*.0005,20.001))} for i in range(2)]}).encode()
    dataset = _upload(client, project, admin, 'neighbors.geojson', body)
    client.post(f'/api/projects/{project}/datasets/{dataset["id"]}/mapping', headers=headers(admin),
                json={'mapping':{'parcel_id':'parcel_id'},'confirm':True})
    features = client.get(f'/api/projects/{project}/datasets/{dataset["id"]}/features', headers=headers(admin)).json()
    for feature in features:
        response = client.post(f'/api/projects/{project}/features/{feature["id"]}/baseline', headers=headers(reviewer),
                               json={'geometry_source_id':feature['id'],'attribute_source_id':feature['id'],'rationale':'Candidate baseline'})
        assert response.status_code == 200, response.text
    validation = client.post(f'/api/projects/{project}/validate', headers=headers(reviewer)).json()
    assert not validation['valid'] and validation['neighbor_pairs_checked']
    assert any('overlap_m2' in f for f in validation['failures'])
    assert client.post(f'/api/projects/{project}/publish', headers=headers(reviewer)).status_code == 409


def test_all_vector_formats_preserve_uploads(client, auth_token, tmp_path):
    import fiona
    import zipfile
    import io
    project, admin, _ = _project_with_reviewer(client, auth_token, 'all-formats')
    for driver, suffix in [('GPKG','gpkg'),('ESRI Shapefile','shp')]:
        path = tmp_path / f'parcels.{suffix}'
        with fiona.open(path, 'w', driver=driver, schema={'geometry':'Polygon','properties':{'parcel_id':'str'}}, crs='EPSG:4326') as sink:
            sink.write({'geometry':mapping(box(73,20,73.001,20.001)), 'properties':{'parcel_id':'0001-B'}})
        if suffix == 'shp':
            output = io.BytesIO()
            with zipfile.ZipFile(output,'w') as archive:
                for part in tmp_path.glob('parcels.*'):
                    if part.suffix != '.gpkg': archive.write(part,part.name)
            data, filename = output.getvalue(), 'parcels.zip'
        else:
            data, filename = path.read_bytes(), path.name
        response = client.post(f'/api/projects/{project}/datasets/upload', headers=headers(admin), files={'file':(filename,data,'application/octet-stream')})
        assert response.status_code == 201, response.text
        dataset = response.json()
        assert dataset['normalized_count'] == 1 and dataset['normalized_crs'] == 'EPSG:4326'
        assert client.get(f'/api/projects/{project}/datasets/{dataset["id"]}/raw', headers=headers(admin)).content == data


def test_postgis_native_storage_and_gist(db):
    import pytest
    from sqlalchemy import text
    if db.bind.dialect.name != 'postgresql':
        pytest.skip('Run with TEST_DATABASE_URL for native PostGIS assertion')
    assert db.scalar(text('SELECT count(*) FROM source_features WHERE spatial_geometry IS NOT NULL AND ST_SRID(spatial_geometry)=4326')) > 0
    assert db.scalar(text("SELECT count(*) FROM pg_indexes WHERE tablename='source_features' AND indexdef ILIKE '%using gist%'")) > 0


def test_split_candidates_abstain_and_do_not_form_identity(client, auth_token):
    project, admin, reviewer = _project_with_reviewer(client, auth_token, 'split-case')
    left = _upload(client, project, admin, 'whole.geojson', _geojson('0001',73,20))
    body = json.dumps({'type':'FeatureCollection','features':[
        {'type':'Feature','id':f'{i}-part','properties':{'parcel_id':'0001'},'geometry':mapping(box(73+i*.0005,20,73+.0005+i*.0005,20.001))} for i in range(2)]}).encode()
    right = _upload(client, project, admin, 'split.geojson', body)
    response = client.post(f'/api/projects/{project}/match',headers=headers(admin),json={'left_dataset_id':left['id'],'right_dataset_id':right['id']})
    proposal = response.json()['proposals'][0]
    assert proposal['status'] == 'ambiguous' and proposal['evidence']['possible_split_merge']
    rejected = client.post(f'/api/projects/{project}/reviews/match/{proposal["id"]}',headers=headers(reviewer),json={'decision':'accepted','rationale':'Not a one-to-one match','expected_revision':1})
    assert rejected.status_code == 409


def test_shared_number_distant_same_village_is_unmatched(client, auth_token):
    project, admin, _ = _project_with_reviewer(client, auth_token, 'distant-namespace')
    left = _upload(client, project, admin, 'left.geojson', _geojson('0001',73,20,'same'))
    right = _upload(client, project, admin, 'right.geojson', _geojson('0001',74,21,'same'))
    response = client.post(f'/api/projects/{project}/match',headers=headers(admin),json={'left_dataset_id':left['id'],'right_dataset_id':right['id']})
    assert response.json()['unmatched_left'] == 1


def test_policy_version_invalidation_and_role_cap(client, auth_token):
    project, admin, reviewer, *_ = prepare_pair(client, auth_token, 'policy-version')
    assert client.post(f'/api/projects/{project}/validate',headers=headers(reviewer)).json()['valid']
    policy = client.get(f'/api/projects/{project}/policy',headers=headers(admin)).json()
    updated = client.post(f'/api/projects/{project}/policy',headers=headers(reviewer),json={**policy,'expected_version':policy['version'],'geometry_tolerance':1.2})
    assert updated.status_code == 200 and updated.json()['version'] == 1
    assert client.post(f'/api/projects/{project}/publish',headers=headers(reviewer)).status_code == 409
    assert client.post(f'/api/projects/{project}/policy',headers=headers(reviewer),json={**policy,'expected_version':0}).status_code == 409
    processor = auth_token('processor')
    own = client.post('/api/projects',headers=headers(processor),json={'name':'processor-owned'}).json()['id']
    assert client.post(f'/api/projects/{own}/publish',headers=headers(processor)).status_code == 403


def test_implausible_declared_crs_is_quarantined(client, auth_token):
    project, admin, reviewer = _project_with_reviewer(client, auth_token, "implausible")
    dataset = _upload(client, project, admin, "bad.geojson", _geojson("1", 1000, 2000))
    features = client.get(f"/api/projects/{project}/datasets/{dataset['id']}/features", headers=headers(admin)).json()
    assert features[0]["geometry"] is None
    assert features[0]["status"] == "quarantined"
    assert not client.post(f"/api/projects/{project}/validate", headers=headers(reviewer)).json()["valid"]
    assert client.post(f"/api/projects/{project}/publish", headers=headers(reviewer)).status_code == 409


def test_topology_contains_invalid_duplicate_and_containment_without_replay(client, auth_token):
    project, admin, _ = _project_with_reviewer(client, auth_token, "topology-integrity")
    geometries = [mapping(box(73, 20, 73.01, 20.01)), mapping(box(73.002, 20.002, 73.003, 20.003))]
    geometries += [geometries[1], {"type": "Polygon", "coordinates": [[[73,20],[73.01,20.01],[73,20.01],[73.01,20],[73,20]]]}]
    body = json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"parcel_id": str(i)}, "geometry": geom} for i, geom in enumerate(geometries)]}).encode()
    dataset = _upload(client, project, admin, "topology.geojson", body)
    path = f"/api/projects/{project}/topology/{dataset['id']}"
    assert client.post(path, headers=headers(admin)).status_code == 200
    conflicts = client.get(f"/api/projects/{project}/conflicts", headers=headers(admin)).json()
    assert {c['type'] for c in conflicts} == {"invalid_geometry", "duplicate_geometry", "overlap"}
    assert client.post(path, headers=headers(admin)).status_code == 200
    assert len(client.get(f"/api/projects/{project}/conflicts", headers=headers(admin)).json()) == len(conflicts)
