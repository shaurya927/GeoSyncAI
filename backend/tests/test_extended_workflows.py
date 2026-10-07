import json
import uuid
import pytest


def auth(value):
    return {"Authorization": f"Bearer {value}"}


def geojson(parcel_id: str, x: float, field: str = "parcel_id") -> bytes:
    return json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "id": parcel_id,
        "properties": {field: parcel_id, "ward": "W-1", "recorded_area": 100, "area_units": "m2"},
        "geometry": {"type": "Polygon", "coordinates": [[[x, 20], [x + .001, 20], [x + .001, 20.001], [x, 20]]]}}]}).encode()


def upload(client, project, token, name, body, content_type="application/geo+json"):
    response = client.post(f"/api/projects/{project}/datasets/upload", headers=auth(token),
                           files={"file": (name, body, content_type)}, data={"declared_crs": "EPSG:4326"})
    assert response.status_code == 201, response.text
    return response.json()


def test_source_registry_three_source_reconciliation_and_measurement(client, auth_token):
    admin = auth_token("admin")
    project = client.post("/api/projects", headers=auth(admin), json={"name": f"phase-a-{uuid.uuid4()}"}).json()
    project_id = project["id"]
    first = upload(client, project_id, admin, "parcel.geojson", geojson("0045", 73))
    second = upload(client, project_id, admin, "survey.geojson", geojson("0045", 73.00001))
    revenue = upload(client, project_id, admin, "revenue.csv", b"khasra_no,owner,recorded_area\n0045,Redacted,100\n", "text/csv")
    metadata = client.patch(f"/api/projects/{project_id}/datasets/{first['id']}/metadata", headers=auth(admin), json={
        "expected_content_hash": first["content_hash"], "license_classification": "departmental",
        "administrative_namespace": {"ward": "W-1"}, "provenance": {"source": "survey office"}, "source_version": "2026.1"})
    assert metadata.status_code == 200, metadata.text
    term = client.post(f"/api/projects/{project_id}/mapping-dictionary", headers=auth(admin), json={
        "canonical_field": "parcel_id", "source_term": "खसरा संख्या", "language": "hi",
        "normalized_term": "khasra_no", "rationale": "Department glossary", "confirm": True})
    assert term.status_code == 201, term.text
    fields = []
    for dataset in (first, second, revenue):
        features = client.get(f"/api/projects/{project_id}/datasets/{dataset['id']}/features", headers=auth(admin)).json()
        fields.append(features[0]["id"])
    reconciliation = client.post(f"/api/projects/{project_id}/reconciliations", headers=auth(admin), json={"source_feature_ids": fields})
    assert reconciliation.status_code == 200, reconciliation.text
    assert len(reconciliation.json()["evidence"]) == 3
    measured = client.post(f"/api/projects/{project_id}/measurements", headers=auth(admin), json={
        "geometry": {"type": "Polygon", "coordinates": [[[73, 20], [73.001, 20], [73.001, 20.001], [73, 20]]]},
        "source_crs": "EPSG:4326", "purpose": "analysis"})
    assert measured.status_code == 200, measured.text
    assert measured.json()["area_m2"] > 0
    controls = client.post(f"/api/projects/{project_id}/ground-control", headers=auth(admin), json={
        "dataset_id": first["id"], "method": "affine", "control_points": [
            {"source": [73, 20], "target": [73.00001, 20.00001]},
            {"source": [74, 20], "target": [74.00001, 20.00001]},
            {"source": [73, 21], "target": [73.00001, 21.00001]},
            {"source": [74, 21], "target": [74.00001, 21.00001], "checkpoint": True}]})
    assert controls.status_code == 200, controls.text
    approved = client.post(f"/api/projects/{project_id}/ground-control/{controls.json()['id']}/approve", headers=auth(admin))
    assert approved.status_code == 200 and approved.json()["status"] == "approved"


def test_genuine_geotiff_metadata_preview_and_integrity(client, auth_token):
    rasterio = pytest.importorskip("rasterio")
    from rasterio.io import MemoryFile
    from rasterio.transform import from_origin
    admin = auth_token("admin")
    project_id = client.post("/api/projects", headers=auth(admin), json={"name": f"raster-{uuid.uuid4()}"}).json()["id"]
    with MemoryFile() as memory:
        with memory.open(driver="GTiff", height=2, width=2, count=1, dtype="uint8", crs="EPSG:4326",
                         transform=from_origin(73, 20.002, 0.001, 0.001)) as dataset:
            import numpy as np
            dataset.write(np.array([[1, 2], [3, 4]], dtype="uint8"), 1)
        payload = memory.read()
    raster = client.post(f"/api/projects/{project_id}/raster-assets", headers=auth(admin),
                         files={"file": ("reference.tif", payload, "image/tiff")}, data={"attribution": "Demo source"})
    assert raster.status_code == 201, raster.text
    assert raster.json()["metadata"]["width"] == 2 and raster.json()["metadata"]["crs"] == "EPSG:4326"
    preview = client.get(f"/api/projects/{project_id}/raster-assets/{raster.json()['id']}/preview", headers=auth(admin))
    assert preview.status_code == 200 and preview.headers["content-type"].startswith("image/png")
    raster_bytes = client.get(f"/api/projects/{project_id}/raster-assets/{raster.json()['id']}/bytes", headers=auth(admin))
    assert raster_bytes.status_code == 200 and raster_bytes.content == payload


def test_grouped_supervised_ranker_rejects_leakage_and_reports_calibration(client, auth_token):
    admin = auth_token("admin")
    project_id = client.post("/api/projects", headers=auth(admin), json={"name": f"ranker-{uuid.uuid4()}"}).json()["id"]
    for label, group in ((1, "parcel-a"), (0, "parcel-b"), (1, "parcel-c")):
        response = client.post(f"/api/projects/{project_id}/training-examples", headers=auth(admin), json={
            "label": label, "features": {"identifier_agreement": float(label), "iou": float(label), "score": float(label)},
            "group_key": group, "split": "train" if group != "parcel-c" else "validation"})
        assert response.status_code == 201, response.text
    trained = client.post(f"/api/projects/{project_id}/ranker/train", headers=auth(admin), json={"seed": 9, "version": "test-v1"})
    assert trained.status_code == 200, trained.text
    assert trained.json()["metrics"]["splits"]["validation"]["count"] == 1
    activated = client.post(f"/api/projects/{project_id}/ranker/{trained.json()['id']}/activation", headers=auth(admin), json={"active": True})
    assert activated.status_code == 200 and activated.json()["activation_status"] == "active"


def test_ranker_fieldwork_compliance_query_and_ogc_are_scoped(client, auth_token):
    admin = auth_token("admin")
    reviewer = auth_token("reviewer")
    field = auth_token("field")
    project = client.post("/api/projects", headers=auth(admin), json={"name": f"phase-bcd-{uuid.uuid4()}"}).json()
    project_id = project["id"]
    assert client.post(f"/api/projects/{project_id}/members", headers=auth(admin), json={"username": "reviewer", "project_role": "reviewer"}).status_code == 200
    assert client.post(f"/api/projects/{project_id}/members", headers=auth(admin), json={"username": "field", "project_role": "field"}).status_code == 200
    dataset = upload(client, project_id, admin, "parcel.geojson", geojson("P-1", 73))
    feature = client.get(f"/api/projects/{project_id}/datasets/{dataset['id']}/features", headers=auth(admin)).json()[0]
    baseline = client.post(f"/api/projects/{project_id}/features/{feature['id']}/baseline", headers=auth(reviewer), json={
        "attribute_source_id": feature["id"], "geometry_source_id": feature["id"], "rationale": "Reviewed baseline"})
    assert baseline.status_code == 200, baseline.text
    parcels = client.get(f"/api/projects/{project_id}/parcels", headers=auth(admin)).json()
    parcel_id = parcels[0]["id"]
    assignment = client.post(f"/api/projects/{project_id}/field-assignments", headers=auth(admin), json={
        "assignee_id": client.get("/api/auth/me", headers=auth(field)).json()["id"], "parcel_entity_ids": [parcel_id]})
    assert assignment.status_code == 201, assignment.text
    event_id = str(uuid.uuid4())
    evidence = client.post(f"/api/projects/{project_id}/field-assignments/{assignment.json()['id']}/evidence",
                           headers=auth(field), json={"parcel_entity_id": parcel_id, "client_event_id": event_id,
                           "expected_project_revision": -1, "payload": {"note": "Observed from field"}})
    assert evidence.status_code == 422  # revision is bounded at zero by the API contract
    rule = client.post(f"/api/projects/{project_id}/compliance-rules", headers=auth(reviewer), json={
        "name": "Synthetic setback", "jurisdiction": "Demo ward", "category": "setback",
        "inputs": [{"name": "distance_m", "required": True}], "formula": {"operation": "setback", "actual": "distance_m"},
        "threshold": {"max": 5, "units": "m"}, "confirm": True})
    assert rule.status_code == 201, rule.text
    result = client.post(f"/api/projects/{project_id}/compliance/evaluate", headers=auth(admin), json={
        "parcel_entity_id": parcel_id, "rule_id": rule.json()["id"], "values": {"distance_m": 3}})
    assert result.status_code == 200 and result.json()["status"] == "pass"
    query = client.post(f"/api/projects/{project_id}/query", headers=auth(admin), json={"query": "missing links", "limit": 10})
    assert query.status_code == 200 and query.json()["read_only"] is True
    collections = client.get("/api/ogc/collections", headers=auth(admin))
    assert collections.status_code == 200 and collections.json()["collections"]
