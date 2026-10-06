import json
import hashlib
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
import uuid

from app.models import CitizenGrant, Dataset, FieldAssignment, Job, ParcelEntity
from app.verify_artifact import digest


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def geojson(parcel_id: str = "P-1") -> bytes:
    return json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "id": parcel_id,
        "properties": {"parcel_id": parcel_id, "owner_private": "PRIVATE-OWNER", "ward": "W-1"},
        "geometry": {"type": "Polygon", "coordinates": [[[73, 20], [73.001, 20], [73.001, 20.001], [73, 20]]]}}]}).encode()


def test_limited_roles_cannot_read_source_features_or_create_projects(client, auth_token):
    admin = auth_token("admin"); field = auth_token("field"); citizen = auth_token("citizen")
    project = client.post("/api/projects", headers=headers(admin), json={"name": f"access-{uuid.uuid4()}"}).json()
    project_id = project["id"]
    for username, role in (("field", "field"), ("citizen", "citizen")):
        added = client.post(f"/api/projects/{project_id}/members", headers=headers(admin),
                            json={"username": username, "project_role": role})
        assert added.status_code == 200, added.text
    uploaded = client.post(f"/api/projects/{project_id}/datasets/upload", headers=headers(admin),
                           files={"file": ("restricted.geojson", geojson(), "application/geo+json")},
                           data={"declared_crs": "EPSG:4326"}).json()
    restricted = client.patch(f"/api/projects/{project_id}/datasets/{uploaded['id']}/metadata", headers=headers(admin),
                              json={"access_classification": "restricted", "expected_content_hash": uploaded["content_hash"]})
    assert restricted.status_code == 200
    for token in (field, citizen):
        assert client.get(f"/api/projects/{project_id}/datasets/{uploaded['id']}/features", headers=headers(token)).status_code == 403
        assert client.get(f"/api/projects/{project_id}/datasets/{uploaded['id']}/raw", headers=headers(token)).status_code == 403
        assert client.get(f"/api/ogc/collections/{project_id}/items", headers=headers(token)).status_code == 403
        capabilities = client.get(f"/api/projects/{project_id}/capabilities", headers=headers(token))
        assert capabilities.status_code == 200 and capabilities.json()["read_departmental"] is False
    assert client.post("/api/projects", headers=headers(citizen), json={"name": "blocked"}).status_code == 403
    assert client.get(f"/api/projects/{project_id}", headers=headers(citizen)).status_code == 200


def test_measurement_rejects_geographic_target_and_converts_feet(client, auth_token):
    admin = auth_token("admin")
    project_id = client.post("/api/projects", headers=headers(admin), json={"name": f"measure-{uuid.uuid4()}"}).json()["id"]
    geometry = {"type": "Polygon", "coordinates": [[[73, 20], [73.001, 20], [73.001, 20.001], [73, 20]]]}
    geographic = client.post(f"/api/projects/{project_id}/measurements", headers=headers(admin), json={
        "geometry": geometry, "source_crs": "EPSG:4326", "analysis_crs": "EPSG:4326"})
    assert geographic.status_code == 422
    feet = client.post(f"/api/projects/{project_id}/measurements", headers=headers(admin), json={
        "geometry": {"type": "Polygon", "coordinates": [[[240000, 2200000], [240100, 2200000], [240100, 2200100], [240000, 2200100], [240000, 2200000]]]},
        "source_crs": "+proj=utm +zone=43 +datum=WGS84 +units=ft +no_defs", "analysis_crs": "+proj=utm +zone=43 +datum=WGS84 +units=ft +no_defs"})
    assert feet.status_code == 200, feet.text
    assert feet.json()["units"] == "metres"
    assert "foot" in feet.json()["analysis_units"].casefold()
    assert 900 < feet.json()["area_m2"] < 960


def test_ground_control_excludes_checkpoint_and_creates_approved_dataset(client, auth_token, db):
    admin = auth_token("admin"); reviewer = auth_token("reviewer")
    project_id = client.post("/api/projects", headers=headers(admin), json={"name": f"gcp-{uuid.uuid4()}"}).json()["id"]
    assert client.post(f"/api/projects/{project_id}/members", headers=headers(admin),
                       json={"username": "reviewer", "project_role": "reviewer"}).status_code == 200
    uploaded = client.post(f"/api/projects/{project_id}/datasets/upload", headers=headers(admin),
                           files={"file": ("gcp.geojson", geojson(), "application/geo+json")},
                           data={"declared_crs": "EPSG:4326"}).json()
    points = [
        {"source": [73, 20], "target": [83, 40]},
        {"source": [74, 20], "target": [84, 40]},
        {"source": [73, 21], "target": [83, 41]},
        {"source": [74, 21], "target": [84, 41], "checkpoint": True},
    ]
    session = client.post(f"/api/projects/{project_id}/ground-control", headers=headers(admin), json={
        "dataset_id": uploaded["id"], "method": "similarity", "source_crs": "EPSG:4326",
        "target_crs": "EPSG:4326", "control_points": points, "max_checkpoint_residual": 0.01})
    assert session.status_code == 200, session.text
    result = session.json()
    assert result["residuals"]["fitting_count"] == 3 and result["residuals"]["checkpoint_count"] == 1
    approved = client.post(f"/api/projects/{project_id}/ground-control/{result['id']}/approve",
                           headers=headers(reviewer), json={"expected_revision": 1, "rationale": "Independent check passed"})
    assert approved.status_code == 200, approved.text
    aligned_id = approved.json()["approved_dataset_id"]
    assert approved.json()["status"] == "approved" and aligned_id
    aligned = db.get(Dataset, aligned_id)
    original = db.get(Dataset, uploaded["id"])
    assert aligned.content_hash == original.content_hash and aligned.raw_path == original.raw_path


def test_ground_control_checkpoint_is_independent_and_blocks_bad_fit(client, auth_token):
    admin = auth_token("admin")
    project_id = client.post("/api/projects", headers=headers(admin), json={"name": f"controls-{uuid.uuid4()}"}).json()["id"]
    dataset = client.post(f"/api/projects/{project_id}/datasets/upload", headers=headers(admin),
                          files={"file": ("source.geojson", geojson(), "application/geo+json")},
                          data={"declared_crs": "EPSG:4326"}).json()
    body = {"dataset_id": dataset["id"], "method": "affine", "source_crs": "EPSG:4326", "target_crs": "EPSG:4326",
            "max_checkpoint_residual": 0.1, "control_points": [
                {"source": [0, 0], "target": [1, 2]}, {"source": [1, 0], "target": [2, 2]},
                {"source": [0, 1], "target": [1, 3]}, {"source": [2, 2], "target": [99, 99], "checkpoint": True}]}
    created = client.post(f"/api/projects/{project_id}/ground-control", headers=headers(admin), json=body)
    assert created.status_code == 200, created.text
    assert created.json()["residuals"]["fitting_count"] == 3
    assert created.json()["residuals"]["checkpoint_count"] == 1
    assert created.json()["residuals"]["checkpoint_max"] > 1
    approved = client.post(f"/api/projects/{project_id}/ground-control/{created.json()['id']}/approve",
                           headers=headers(admin), json={"expected_revision": 1, "rationale": "Should fail independent checkpoint"})
    assert approved.status_code == 409


def test_compliance_operator_and_expiry_are_enforced(client, auth_token, db):
    admin = auth_token("admin"); citizen = auth_token("citizen")
    project_id = client.post("/api/projects", headers=headers(admin), json={"name": f"rules-{uuid.uuid4()}"}).json()["id"]
    assert client.post(f"/api/projects/{project_id}/members", headers=headers(admin),
                       json={"username": "citizen", "project_role": "citizen"}).status_code == 200
    rule = client.post(f"/api/projects/{project_id}/compliance-rules", headers=headers(admin), json={
        "name": "Minimum setback", "jurisdiction": "demo", "category": "setback", "operator": ">=", "units": "m",
        "inputs": [{"name": "distance_m", "units": "m", "required": True}], "formula": {"actual": "distance_m"},
        "threshold": {"value": 5}, "confirm": True})
    assert rule.status_code == 201, rule.text
    assert rule.json()["threshold"]["value"] == 5
    parcel = ParcelEntity(project_id=project_id); db.add(parcel); db.commit()
    below = client.post(f"/api/projects/{project_id}/compliance/evaluate", headers=headers(admin), json={
        "parcel_entity_id": parcel.id, "rule_id": rule.json()["id"], "values": {"distance_m": 3}})
    above = client.post(f"/api/projects/{project_id}/compliance/evaluate", headers=headers(admin), json={
        "parcel_entity_id": parcel.id, "rule_id": rule.json()["id"], "values": {"distance_m": 6}})
    assert below.status_code == 200 and below.json()["status"] == "potential_violation"
    assert above.status_code == 200 and above.json()["status"] == "pass"
    grant = CitizenGrant(project_id=project_id, citizen_id=client.get("/api/auth/me", headers=headers(citizen)).json()["id"],
                         parcel_entity_id=parcel.id, fields=["ward"], status="active",
                         expires_at=datetime.now(timezone.utc) - timedelta(days=1), granted_by=client.get("/api/auth/me", headers=headers(admin)).json()["id"])
    db.add(grant); db.commit()
    records = client.get(f"/api/projects/{project_id}/citizen-records", headers=headers(citizen))
    assert records.status_code == 200 and records.json() == []


def test_job_lease_rejects_second_owner_and_queued_cancel_is_terminal(client, auth_token, db):
    from app.tasks import execute_job
    admin = auth_token("admin")
    project_id = client.post("/api/projects", headers=headers(admin), json={"name": f"lease-{uuid.uuid4()}"}).json()["id"]
    running = Job(project_id=project_id, job_type="match", payload={}, status="running", owner_token="live-owner",
                  lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5), attempts=1, max_attempts=3)
    queued = Job(project_id=project_id, job_type="match", payload={}, status="queued", max_attempts=3)
    db.add_all([running, queued]); db.commit()
    execute_job(running.id)
    db.expire_all(); db.refresh(running)
    assert running.status == "running" and running.owner_token == "live-owner" and running.attempts == 1
    cancelled = client.post(f"/api/projects/{project_id}/jobs/{queued.id}/cancel", headers=headers(admin))
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"


def test_independent_artifact_cli_detects_output_tampering(tmp_path):
    manifest = {"version": 1, "features": [{"id": "parcel-1"}]}
    output = {"type": "FeatureCollection", "features": [{"id": "parcel-1", "geometry": None,
        "properties": {"value": 10, "_lineage": {"sources": []}}}]}
    manifest_path = tmp_path / "manifest.json"; output_path = tmp_path / "published.geojson"; source_path = tmp_path / "source.bin"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    output_path.write_text(json.dumps(output), encoding="utf-8")
    source_path.write_bytes(b"immutable-source")
    normalized_output = [{"parcel_entity_id": "parcel-1", "geometry": None, "attributes": {"value": 10}, "lineage": {"sources": []}}]
    expected_source = hashlib.sha256(source_path.read_bytes()).hexdigest()
    command = [sys.executable, "-m", "app.verify_artifact", "--manifest", str(manifest_path),
               "--expected-manifest-sha256", digest(manifest), "--geojson", str(output_path),
               "--expected-output-sha256", digest(normalized_output), "--source", f"{source_path}={expected_source}"]
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])}
    result = subprocess.run(command, capture_output=True, text=True, check=False, env=env)
    assert result.returncode == 0 and json.loads(result.stdout)["valid"] is True
    output_path.write_text(json.dumps({**output, "features": []}), encoding="utf-8")
    tampered = subprocess.run(command, capture_output=True, text=True, check=False, env=env)
    assert tampered.returncode == 1 and json.loads(tampered.stdout)["output_valid"] is False


def test_one_parent_split_creates_successors_and_publishes_lineage(client, auth_token, db):
    admin = auth_token("admin"); reviewer = auth_token("reviewer")
    project_id = client.post("/api/projects", headers=headers(admin), json={"name": f"split-{uuid.uuid4()}"}).json()["id"]
    assert client.post(f"/api/projects/{project_id}/members", headers=headers(admin),
                       json={"username": "reviewer", "project_role": "reviewer"}).status_code == 200
    body = json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "id": "PARENT",
        "properties": {"parcel_id": "PARENT"}, "geometry": {"type": "Polygon",
        "coordinates": [[[73, 20], [73.002, 20], [73.002, 20.001], [73, 20.001], [73, 20]]]}}]}).encode()
    dataset = client.post(f"/api/projects/{project_id}/datasets/upload", headers=headers(admin),
                          files={"file": ("parent.geojson", body, "application/geo+json")},
                          data={"declared_crs": "EPSG:4326"}).json()
    feature = client.get(f"/api/projects/{project_id}/datasets/{dataset['id']}/features", headers=headers(admin)).json()[0]
    assert client.post(f"/api/projects/{project_id}/datasets/{dataset['id']}/mapping", headers=headers(admin),
                       json={"mapping": {"parcel_id": "parcel_id"}, "confirm": True}).status_code == 200
    baseline = client.post(f"/api/projects/{project_id}/features/{feature['id']}/baseline", headers=headers(reviewer),
                           json={"geometry_source_id": feature["id"], "attribute_source_id": feature["id"], "rationale": "Parent baseline"})
    assert baseline.status_code == 200, baseline.text
    parent = client.get(f"/api/projects/{project_id}/parcels", headers=headers(reviewer)).json()[0]["id"]
    children = {
        "left": {"type": "Polygon", "coordinates": [[[73, 20], [73.001, 20], [73.001, 20.001], [73, 20.001], [73, 20]]]},
        "right": {"type": "Polygon", "coordinates": [[[73.001, 20], [73.002, 20], [73.002, 20.001], [73.001, 20.001], [73.001, 20]]]}}
    created = client.post(f"/api/projects/{project_id}/geometry-changes", headers=headers(admin), json={
        "operation": "split", "parcel_entity_ids": [parent], "draft_geometries": children,
        "attribute_source_id": feature["id"], "rationale": "Reviewed parent split"})
    assert created.status_code == 200, created.text
    assert len(created.json()["successor_ids"]) == 2
    approved = client.post(f"/api/projects/{project_id}/geometry-changes/{created.json()['id']}/decision",
                           headers=headers(reviewer), json={"decision": "approved", "expected_revision": 1, "rationale": "Approved split"})
    assert approved.status_code == 200, approved.text
    active = client.get(f"/api/projects/{project_id}/parcels", headers=headers(reviewer)).json()
    assert len(active) == 2
    validation = client.post(f"/api/projects/{project_id}/validate", headers=headers(reviewer))
    assert validation.status_code == 200 and validation.json()["valid"] is True, validation.text
    published = client.post(f"/api/projects/{project_id}/publish", headers=headers(reviewer))
    assert published.status_code == 200, published.text
    exported = client.get(f"/api/projects/{project_id}/versions/{published.json()['id']}/export?format=geojson", headers=headers(reviewer)).json()
    assert len(exported["features"]) == 2
    assert all(item["properties"]["_lineage"]["geometry_changes"] for item in exported["features"])
    verified = client.get(f"/api/projects/{project_id}/versions/{published.json()['id']}/verify", headers=headers(reviewer))
    assert verified.status_code == 200 and verified.json()["valid"] is True
    raw_path = Path(db.get(Dataset, dataset["id"]).raw_path)
    raw_path.write_bytes(raw_path.read_bytes() + b"tampered")
    tampered = client.get(f"/api/projects/{project_id}/versions/{published.json()['id']}/verify", headers=headers(reviewer))
    assert tampered.status_code == 200 and tampered.json()["valid"] is False and tampered.json()["source_bytes_valid"] is False
    page = client.get(f"/api/ogc/collections/{project_id}/items", headers=headers(reviewer), params={"limit": 1})
    assert page.status_code == 200 and page.json()["numberMatched"] == 2 and page.json()["numberReturned"] == 1
    bounded = client.get(f"/api/ogc/collections/{project_id}/items", headers=headers(reviewer),
                         params={"bbox": "73,20,73.0005,20.001", "limit": 10})
    assert bounded.status_code == 200 and bounded.json()["numberMatched"] == 1
