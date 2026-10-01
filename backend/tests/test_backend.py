import hashlib
import json
import time

from app.models import ChangeProposal, Project


def headers(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


def test_project_scoped_authorization(client, auth_token):
    admin = auth_token("admin")
    viewer = auth_token("viewer")
    created = client.post("/api/projects", headers=headers(admin), json={"name": "private-project"})
    assert created.status_code == 200
    project_id = created.json()["id"]

    assert client.get(f"/api/projects/{project_id}", headers=headers(viewer)).status_code == 403
    assert client.post(f"/api/projects/{project_id}/datasets", headers=headers(viewer),
                       json={"name": "blocked"}).status_code == 403
    assert client.post("/api/projects", headers=headers(viewer), json={"name": "viewer-owned"}).status_code == 403
    seeded = client.get("/api/projects", headers=headers(viewer)).json()
    seeded_id = next(p["id"] for p in seeded if p["name"] == "Synthetic demonstration")
    assert client.post(f"/api/projects/{seeded_id}/datasets", headers=headers(viewer),
                       json={"name": "viewer-cannot-write"}).status_code == 403


def test_upload_preserves_raw_hash_and_reports_unknown_crs(client, auth_token):
    processor = auth_token("processor")
    # The seeded synthetic project includes processor membership.
    projects = client.get("/api/projects", headers=headers(processor)).json()
    project_id = next(p["id"] for p in projects if p["name"] == "Synthetic demonstration")
    body = {"type": "FeatureCollection", "features": [{"type": "Feature", "id": "A-1",
            "properties": {"parcel_id": "A-1"}, "geometry": {"type": "Polygon",
            "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}}]}
    raw = json.dumps(body).encode()
    response = client.post(f"/api/projects/{project_id}/datasets/upload", headers=headers(processor),
                           files={"file": ("parcels.geojson", raw, "application/geo+json")},
                           data={"name": "uploaded-test"})
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["content_hash"] == hashlib.sha256(raw).hexdigest()
    assert result["status"] == "needs_crs_review"
    assert any("CRS is unknown" in warning for warning in result["validation_report"]["warnings"])
    downloaded = client.get(f"/api/projects/{project_id}/datasets/{result['id']}/raw", headers=headers(processor))
    assert downloaded.status_code == 200
    assert downloaded.content == raw


def test_unapproved_boundary_change_blocks_publication(client, auth_token, db):
    admin = auth_token("admin")
    reviewer = auth_token("reviewer")
    created = client.post("/api/projects", headers=headers(admin), json={"name": "approval-project"})
    assert created.status_code == 200
    project_id = created.json()["id"]
    added = client.post(f"/api/projects/{project_id}/members", headers=headers(admin),
                        json={"username": "reviewer", "project_role": "reviewer"})
    assert added.status_code == 200, added.text

    proposal = ChangeProposal(project_id=project_id, change_type="geometry_changed", boundary_change=True,
                              status="proposed", evidence={"test": True})
    db.add(proposal)
    db.commit()
    db.refresh(proposal)
    blocked = client.post(f"/api/projects/{project_id}/publish", headers=headers(reviewer))
    assert blocked.status_code == 409
    assert "Boundary changes" in blocked.json()["detail"]

    accepted = client.post(f"/api/projects/{project_id}/reviews/change/{proposal.id}", headers=headers(reviewer),
                           json={"decision": "accepted", "rationale": "Reviewed boundary evidence"})
    assert accepted.status_code == 200, accepted.text
    still_blocked = client.post(f"/api/projects/{project_id}/publish", headers=headers(reviewer))
    assert still_blocked.status_code == 409
    validation = client.post(f"/api/projects/{project_id}/validate", headers=headers(reviewer))
    assert validation.status_code == 200, validation.text
    assert validation.json()["valid"] is True
    published = client.post(f"/api/projects/{project_id}/publish", headers=headers(reviewer))
    assert published.status_code == 200, published.text
    assert published.json()["version"] == 1


def test_synthetic_bootstrap_and_explainable_matching(client, auth_token):
    processor = auth_token("processor")
    projects = client.get("/api/projects", headers=headers(processor)).json()
    project_id = next(p["id"] for p in projects if p["name"] == "Synthetic demonstration")
    generated = client.post(f"/api/projects/{project_id}/bootstrap-synthetic?count=3", headers=headers(processor))
    assert generated.status_code == 200, generated.text
    left_id, right_id = generated.json()["dataset_ids"]
    matched = client.post(f"/api/projects/{project_id}/match", headers=headers(processor), json={
        "left_dataset_id": left_id, "right_dataset_id": right_id, "max_distance": 75
    })
    assert matched.status_code == 200, matched.text
    result = matched.json()
    assert result["matched"] == 3
    assert result["spatial_evidence_used"] is True
    assert all(item["evidence"]["identifier_agreement"] for item in result["proposals"])
    assert all(item["evidence"]["centroid_distance_m"] is not None for item in result["proposals"])


def test_attribute_only_csv_is_processed_without_spatial_claim(client, auth_token):
    processor = auth_token("processor")
    project = next(item for item in client.get("/api/projects", headers=headers(processor)).json()
                    if item["name"] == "Synthetic demonstration")
    raw = b"Khasra_No,Owner_Name,Recorded_Area\n0045,Redacted,1200\n0046,Redacted,980\n"
    response = client.post(f"/api/projects/{project['id']}/datasets/upload", headers=headers(processor),
                           files={"file": ("revenue.csv", raw, "text/csv")})
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["status"] == "processed"
    assert result["normalized_count"] == 2
    assert result["normalized_crs"] is None
    assert result["validation_report"]["attribute_only"] == 2
    features = client.get(f"/api/projects/{project['id']}/datasets/{result['id']}/features",
                          headers=headers(processor))
    assert features.status_code == 200
    assert all(item["status"] == "processed" for item in features.json())


def test_durable_match_job_executes_and_is_idempotent(client, auth_token):
    processor = auth_token("processor")
    project = next(item for item in client.get("/api/projects", headers=headers(processor)).json()
                    if item["name"] == "Synthetic demonstration")
    generated = client.post(f"/api/projects/{project['id']}/bootstrap-synthetic?count=2", headers=headers(processor))
    assert generated.status_code == 200, generated.text
    left_id, right_id = generated.json()["dataset_ids"]
    payload = {"left_dataset_id": left_id, "right_dataset_id": right_id, "max_distance": 75, "ambiguity_margin": 0.08}
    first = client.post(f"/api/projects/{project['id']}/jobs?job_type=match&idempotency_key=job-test-{left_id}-{right_id}",
                        headers=headers(processor), json=payload)
    assert first.status_code == 200, first.text
    job = first.json()
    for _ in range(20):
        if job["status"] == "succeeded":
            break
        time.sleep(0.05)
        job = client.get(f"/api/projects/{project['id']}/jobs/{job['id']}", headers=headers(processor)).json()
    assert job["status"] == "succeeded", job
    second = client.post(f"/api/projects/{project['id']}/jobs?job_type=match&idempotency_key=job-test-{left_id}-{right_id}",
                         headers=headers(processor), json=payload)
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
