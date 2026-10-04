import hashlib
import json
import time

from app.models import ChangeProposal, ParcelSourceLink


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
    assert blocked.status_code == 409

    accepted = client.post(f"/api/projects/{project_id}/reviews/change/{proposal.id}", headers=headers(reviewer),
                            json={"decision": "accepted", "rationale": "Reviewed boundary evidence", "expected_revision": 1})
    assert accepted.status_code == 200, accepted.text
    still_blocked = client.post(f"/api/projects/{project_id}/publish", headers=headers(reviewer))
    assert still_blocked.status_code == 409
    validation = client.post(f"/api/projects/{project_id}/validate", headers=headers(reviewer))
    assert validation.status_code == 200, validation.text
    assert validation.json()["valid"] is False  # no baseline or after geometry was supplied
    published = client.post(f"/api/projects/{project_id}/publish", headers=headers(reviewer))
    assert published.status_code == 409, published.text


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


def _project_with_reviewer(client, auth_token, name: str) -> tuple[str, str, str]:
    admin = auth_token("admin")
    reviewer = auth_token("reviewer")
    created = client.post("/api/projects", headers=headers(admin), json={"name": name})
    assert created.status_code == 200, created.text
    project_id = created.json()["id"]
    member = client.post(f"/api/projects/{project_id}/members", headers=headers(admin),
                         json={"username": "reviewer", "project_role": "reviewer"})
    assert member.status_code == 200, member.text
    return project_id, admin, reviewer


def _geojson(parcel_id: str, x: float, y: float, village: str | None = None) -> bytes:
    properties = {"parcel_id": parcel_id}
    if village:
        properties["village"] = village
    return json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "id": parcel_id,
        "properties": properties, "geometry": {"type": "Polygon",
        "coordinates": [[[x, y], [x + 0.001, y], [x + 0.001, y + 0.001], [x, y + 0.001], [x, y]]]}}]}).encode()


def _upload(client, project_id: str, token: str, filename: str, body: bytes, crs: str | None = "EPSG:4326", capture_date=None):
    data = {"declared_crs": crs} if crs else {}
    if capture_date:
        data['capture_date'] = capture_date
    response = client.post(f"/api/projects/{project_id}/datasets/upload", headers=headers(token),
                           files={"file": (filename, body, "application/geo+json")}, data=data)
    assert response.status_code == 201, response.text
    return response.json()


def test_same_identifier_in_different_villages_remains_unmatched(client, auth_token):
    project_id, admin, _ = _project_with_reviewer(client, auth_token, "namespace-project")
    left = _upload(client, project_id, admin, "left.geojson", _geojson("0045", 0, 0, "alpha"))
    right = _upload(client, project_id, admin, "right.geojson", _geojson("0045", 10, 10, "beta"))
    result = client.post(f"/api/projects/{project_id}/match", headers=headers(admin), json={
        "left_dataset_id": left["id"], "right_dataset_id": right["id"], "max_distance": 75,
    })
    assert result.status_code == 200, result.text
    assert result.json()["matched"] == 0
    assert result.json()["unmatched_left"] == 1


def test_unknown_crs_cannot_be_published_and_confirmation_reprocesses_source(client, auth_token):
    project_id, admin, reviewer = _project_with_reviewer(client, auth_token, "crs-project")
    raw = _geojson("CRS-1", 1000, 2000)
    uploaded = _upload(client, project_id, admin, "unknown.geojson", raw, crs=None)
    assert uploaded["status"] == "needs_crs_review"
    assert client.get(f"/api/projects/{project_id}/datasets/{uploaded['id']}/features",
                      headers=headers(admin)).json()[0]["status"] == "awaiting_crs"
    validation = client.post(f"/api/projects/{project_id}/validate", headers=headers(reviewer))
    assert validation.status_code == 200
    blocked = client.post(f"/api/projects/{project_id}/publish", headers=headers(reviewer))
    assert blocked.status_code == 409
    assert "CRS" in blocked.json()["detail"]

    invalid_confirmation = client.post(f"/api/projects/{project_id}/datasets/{uploaded['id']}/confirm-crs",
                            headers=headers(admin), json={"crs": "EPSG:4326", "reason": "Verified from source metadata"})
    assert invalid_confirmation.status_code == 422, invalid_confirmation.text
    confirmed = client.post(f"/api/projects/{project_id}/datasets/{uploaded['id']}/confirm-crs",
                            headers=headers(admin), json={"crs": "EPSG:3857", "reason": "Source is Web Mercator metres"})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["normalized_crs"] == "EPSG:4326"
    features = client.get(f"/api/projects/{project_id}/datasets/{uploaded['id']}/features",
                          headers=headers(admin)).json()
    assert features[0]["status"] == "processed"
    assert features[0]["original_geometry"]["coordinates"][0][0] == [1000, 2000]
    assert abs(features[0]["geometry"]["coordinates"][0][0][0]) < 1
    assert client.get(f"/api/projects/{project_id}/datasets/{uploaded['id']}/raw",
                      headers=headers(admin)).content == raw


def test_accepted_match_creates_one_canonical_publication_with_both_sources(client, auth_token, db):
    project_id, admin, reviewer = _project_with_reviewer(client, auth_token, "canonical-project")
    left = _upload(client, project_id, admin, "left.geojson", _geojson("P-1", 73, 20))
    right = _upload(client, project_id, admin, "right.geojson", _geojson("P-1", 73, 20))
    for dataset in (left, right):
        assert client.post(f"/api/projects/{project_id}/datasets/{dataset['id']}/mapping", headers=headers(admin),
                           json={"mapping": {"parcel_id": "parcel_id"}, "confirm": True}).status_code == 200
    matched = client.post(f"/api/projects/{project_id}/match", headers=headers(admin), json={
        "left_dataset_id": left["id"], "right_dataset_id": right["id"], "max_distance": 75,
    })
    assert matched.status_code == 200, matched.text
    proposal = matched.json()["proposals"][0]
    decision = client.post(f"/api/projects/{project_id}/reviews/match/{proposal['id']}", headers=headers(reviewer),
                           json={"decision": "accepted", "rationale": "Same source identity and geometry", "expected_revision": 1})
    assert decision.status_code == 200, decision.text
    parcel = client.get(f"/api/projects/{project_id}/parcels", headers=headers(reviewer)).json()[0]
    selected = client.post(f"/api/projects/{project_id}/parcels/{parcel['id']}/selection", headers=headers(reviewer),
                           json={"geometry_source_id": proposal["left_feature_id"], "attribute_source_id": proposal["left_feature_id"],
                                 "rationale": "Approved baseline separately from identity", "expected_revision": 0})
    assert selected.status_code == 200, selected.text
    assert client.post(f"/api/projects/{project_id}/validate", headers=headers(reviewer)).json()["valid"] is True
    published = client.post(f"/api/projects/{project_id}/publish", headers=headers(reviewer))
    assert published.status_code == 200, published.text
    version_id = published.json()["id"]
    exported = client.get(f"/api/projects/{project_id}/versions/{version_id}/export?format=geojson",
                          headers=headers(reviewer))
    assert exported.status_code == 200
    features = exported.json()["features"]
    assert len(features) == 1
    assert len(features[0]["properties"]["_lineage"]["source_feature_ids"]) == 2
    links = db.query(ParcelSourceLink).filter(ParcelSourceLink.project_id == project_id).all()
    assert len(links) == 2


def test_stale_review_is_rejected(client, auth_token):
    project_id, admin, reviewer = _project_with_reviewer(client, auth_token, "review-concurrency-project")
    left = _upload(client, project_id, admin, "left.geojson", _geojson("P-2", 73, 20))
    right = _upload(client, project_id, admin, "right.geojson", _geojson("P-2", 73, 20))
    matched = client.post(f"/api/projects/{project_id}/match", headers=headers(admin), json={
        "left_dataset_id": left["id"], "right_dataset_id": right["id"], "max_distance": 75,
    }).json()
    proposal_id = matched["proposals"][0]["id"]
    accepted = client.post(f"/api/projects/{project_id}/reviews/match/{proposal_id}", headers=headers(reviewer),
                           json={"decision": "accepted", "rationale": "Reviewed", "expected_revision": 1})
    assert accepted.status_code == 200
    stale = client.post(f"/api/projects/{project_id}/reviews/match/{proposal_id}", headers=headers(reviewer),
                        json={"decision": "rejected", "rationale": "Stale tab", "expected_revision": 1})
    assert stale.status_code == 409
