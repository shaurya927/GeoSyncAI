"""Acceptance for the independently reproduced submission blockers."""
import json
import uuid

import pytest
from shapely import union_all
from shapely.geometry import shape


def headers(token):
    return {"Authorization": "Bearer " + token}


@pytest.fixture
def study(client, auth_token):
    tokens = {name: auth_token(name) for name in ("admin", "reviewer", "processor", "viewer", "field", "citizen")}
    admin = headers(tokens["admin"])
    project = client.post("/api/projects", headers=admin, json={"name": "repair-" + str(uuid.uuid4())}).json()["id"]
    prefix = "/api/projects/" + project
    for name in tokens:
        if name != "admin":
            response = client.post(prefix + "/members", headers=admin, json={"username": name, "project_role": name})
            assert response.status_code == 200

    def upload(geometry, name="parcel", classification="internal", ward="W-1", capture="2025-01-01", crs="EPSG:4326"):
        data = json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature",
            "properties": {"parcel_id": name, "recorded_area": 100, "ward": ward}, "geometry": geometry}]}).encode()
        response = client.post(prefix + "/datasets/upload", headers=admin,
            files={"file": (name + ".geojson", data, "application/geo+json")},
            data={"declared_crs": crs, "access_classification": classification, "capture_date": capture})
        assert response.status_code == 201, response.text
        dataset = response.json()
        feature = client.get(prefix + f"/datasets/{dataset['id']}/features", headers=admin).json()[0]
        response = client.post(prefix + f"/datasets/{dataset['id']}/mapping", headers=admin,
            json={"mapping": {"parcel_id": "parcel_id", "recorded_area": "recorded_area", "ward": "ward"}, "confirm": True})
        assert response.status_code == 200, response.text
        return dataset, feature

    return prefix, tokens, upload


RECTANGLE = {"type": "Polygon", "coordinates": [[[73,20],[73.002,20],[73.002,20.002],[73,20.002],[73,20]]]}
CONCAVE = {"type": "Polygon", "coordinates": [[[73,20],[73.002,20],[73.002,20.001],[73.001,20.001],[73.001,20.002],[73,20.002],[73,20]]]}


def baseline(client, prefix, token, feature):
    response = client.post(prefix + f"/features/{feature['id']}/baseline", headers=headers(token),
        json={"geometry_source_id": feature["id"], "attribute_source_id": feature["id"], "rationale": "Explicit synthetic baseline"})
    assert response.status_code == 200, response.text
    return next(row for row in client.get(prefix + "/parcels", headers=headers(token)).json()
                if feature["id"] in row["source_feature_ids"])


def test_query_intents_enforce_capabilities_and_classification(client, study):
    prefix, tokens, upload = study
    upload(RECTANGLE, "private", "restricted")
    public, _ = upload(RECTANGLE, "visible")
    for role in ("citizen", "field"):
        for query in ("area above 1", "missing links", "conflicts", "dated changes"):
            response = client.post(prefix + "/query", headers=headers(tokens[role]), json={"query": query})
            assert response.status_code == 403, (role, query, response.text)
    for role in ("viewer", "processor"):
        result = client.post(prefix + "/query", headers=headers(tokens[role]), json={"query": "area above 1"})
        assert result.status_code == 200
        assert {row["dataset_id"] for row in result.json()["rows"]} == {public["id"]}


def test_query_filters_and_offset_are_applied_before_pagination(client, study):
    prefix, tokens, upload = study
    for name, ward, capture in (("one", "W-1", "2025-01-01"), ("two", "W-1", "2025-01-02"),
                                ("wrong-ward", "W-10", "2025-01-02"), ("old", "W-1", "2024-01-01")):
        upload(RECTANGLE, name=name, ward=ward, capture=capture)
    response = client.post(prefix + "/query", headers=headers(tokens["admin"]), json={
        "query": "area above 1 in ward W-1", "ward": "W-1", "date_from": "2025-01-01",
        "bbox": [72.9,19.9,73.1,20.1], "limit": 1, "offset": 1})
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 2 and len(response.json()["rows"]) == 1
    assert response.json()["rows"][0]["original_id"] in {"one", "two"}


def test_nearby_queries_filter_real_assigned_geometry(client, study):
    prefix, tokens, upload = study
    _, feature = upload(RECTANGLE)
    parcel = baseline(client, prefix, tokens["admin"], feature)
    field_id = client.get("/api/auth/me", headers=headers(tokens["field"])).json()["id"]
    assigned = client.post(prefix + "/field-assignments", headers=headers(tokens["admin"]),
        json={"assignee_id": field_id, "parcel_entity_ids": [parcel["id"]]})
    assert assigned.status_code == 201
    for longitude, latitude, expected in ((73.001,20.001,1),(0,0,0)):
        response = client.post(prefix + "/query", headers=headers(tokens["field"]), json={
            "query": "nearby tasks", "longitude": longitude, "latitude": latitude, "radius_m": 10})
        assert response.status_code == 200 and response.json()["total"] == expected, response.text


def test_geographic_checkpoint_error_is_metres_and_blocks_approval(client, study):
    prefix, tokens, upload = study
    dataset, _ = upload(RECTANGLE)
    controls = [{"source": p, "target": p} for p in ([72.99,19.99],[73.02,19.99],[72.99,20.02])]
    controls.append({"source": [73.001,20.001], "target": [73.001,20.0015], "checkpoint": True})
    response = client.post(prefix + "/ground-control", headers=headers(tokens["admin"]), json={
        "dataset_id": dataset["id"], "method": "translation", "source_crs": "EPSG:4326", "target_crs": "EPSG:4326",
        "max_checkpoint_residual": 1, "control_points": controls})
    assert response.status_code == 200, response.text
    residuals = response.json()["residuals"]
    assert residuals["residual_units"] == "metres" and 55 < residuals["checkpoint_max"] < 56
    approved = client.post(prefix + f"/ground-control/{response.json()['id']}/approve", headers=headers(tokens["reviewer"]),
        json={"expected_revision": 1, "rationale": "Checkpoint must pass a metre threshold"})
    assert approved.status_code == 409


def test_ground_control_enforces_observed_coverage(client, study):
    prefix, tokens, upload = study
    dataset, _ = upload(RECTANGLE)
    response = client.post(prefix + "/ground-control", headers=headers(tokens["admin"]), json={
        "dataset_id": dataset["id"], "method": "translation", "control_points": [
            {"source": [73,20], "target": [73,20]},
            {"source": [73.001,20.001], "target": [73.001,20.001], "checkpoint": True}]})
    assert response.status_code == 200
    assert response.json()["residuals"]["coverage"]["covers_source"] is False
    approved = client.post(prefix + f"/ground-control/{response.json()['id']}/approve", headers=headers(tokens["reviewer"]),
        json={"expected_revision": 1, "rationale": "Cannot approve unsupported extrapolation"})
    assert approved.status_code == 409 and "coverage" in approved.text


def test_projected_feet_checkpoints_are_converted_and_original_crs_preserved(client, study, db):
    from app.models import Dataset
    prefix, tokens, upload = study
    crs = "+proj=utm +zone=43 +datum=WGS84 +units=ft +no_defs"
    geometry = {"type":"Polygon","coordinates":[[[240000,2200000],[240100,2200000],[240100,2200100],[240000,2200100],[240000,2200000]]]}
    dataset, _ = upload(geometry, crs=crs)
    controls = [{"source": p, "target": p} for p in ([239900,2199900],[240500,2199900],[239900,2200500])]
    controls.append({"source":[240000,2200000],"target":[240002,2200000],"checkpoint":True})
    response = client.post(prefix + "/ground-control", headers=headers(tokens["admin"]), json={
        "dataset_id": dataset["id"],"method":"translation","source_crs":crs,"target_crs":crs,"control_points":controls})
    assert response.status_code == 200, response.text
    assert response.json()["residuals"]["checkpoint_max"] == pytest.approx(0.6096)
    approved = client.post(prefix + f"/ground-control/{response.json()['id']}/approve", headers=headers(tokens["reviewer"]),
        json={"expected_revision":1,"rationale":"Independent metric checkpoint and hull checked"})
    assert approved.status_code == 200, approved.text
    aligned = db.get(Dataset, approved.json()["approved_dataset_id"])
    assert aligned.parent_dataset_id == dataset["id"] and aligned.declared_crs == crs
    assert aligned.record_count == 1 and aligned.content_hash == dataset["content_hash"]


@pytest.mark.parametrize("geometry", [CONCAVE, {"type":"Polygon","coordinates":[RECTANGLE["coordinates"][0],
    [[73.0002,20.0002],[73.0002,20.0006],[73.0006,20.0006],[73.0006,20.0002],[73.0002,20.0002]]]}])
def test_true_split_and_union_preserve_concavity_and_holes(client, study, geometry):
    prefix, tokens, upload = study
    _, feature = upload(geometry)
    parcel = baseline(client, prefix, tokens["admin"], feature)
    preview = client.post(prefix + "/geometry-drafts", headers=headers(tokens["admin"]),
        json={"operation":"split","parcel_entity_ids":[parcel["id"]]})
    assert preview.status_code == 200, preview.text
    result = preview.json()
    assert len(result["draft_geometries"]) >= 2
    assert abs(result["measurements"]["area_conservation_delta_m2"]) < 0.01
    recovered = union_all([shape(g) for g in result["draft_geometries"].values()])
    assert recovered.symmetric_difference(shape(geometry)).area < 1e-12
    submitted = client.post(prefix + "/geometry-changes", headers=headers(tokens["admin"]), json={
        "operation":"split","parcel_entity_ids":[parcel["id"]],"draft_geometries":result["draft_geometries"],
        "attribute_source_id":feature["id"],"rationale":"Actual polygon split, preserving holes"})
    assert submitted.status_code == 200, submitted.text
    approved = client.post(prefix + f"/geometry-changes/{submitted.json()['id']}/decision", headers=headers(tokens["reviewer"]),
        json={"decision":"approved","expected_revision":1,"rationale":"Reviewed true cut and conserved area"})
    assert approved.status_code == 200, approved.text
    merged = client.post(prefix + "/geometry-drafts", headers=headers(tokens["admin"]),
        json={"operation":"merge","parcel_entity_ids":submitted.json()["successor_ids"]})
    assert merged.status_code == 200, merged.text
    assert shape(merged.json()["draft_geometries"]["merged"]).symmetric_difference(shape(geometry)).area < 1e-12


def test_restricted_registry_and_processing_use_project_permissions(client, study):
    prefix, tokens, upload = study
    restricted, secret = upload(RECTANGLE, "restricted", "restricted")
    public, _ = upload(RECTANGLE, "ordinary")
    parcel = baseline(client, prefix, tokens["admin"], secret)
    changed = client.post(prefix + "/members", headers=headers(tokens["admin"]),
        json={"username":"reviewer","project_role":"viewer"})
    assert changed.status_code == 200
    for role in ("viewer", "processor", "reviewer"):
        auth = headers(tokens[role])
        datasets = client.get(prefix + "/datasets", headers=auth)
        assert {d["id"] for d in datasets.json()} == {public["id"]}
        assert client.get(prefix + f"/datasets/{restricted['id']}/features", headers=auth).status_code == 403
        assert client.get(prefix + f"/datasets/{restricted['id']}/mapping", headers=auth).status_code == 403
        assert parcel["id"] not in {p["id"] for p in client.get(prefix + "/parcels", headers=auth).json()}
    auth = headers(tokens["processor"])
    assert client.post(prefix + "/match", headers=auth, json={"left_dataset_id":restricted["id"],"right_dataset_id":public["id"]}).status_code == 403
    assert client.post(prefix + "/jobs?job_type=topology", headers=auth, json={"dataset_id":restricted["id"]}).status_code == 403


def test_geometry_neighbor_shared_edge_and_stale_preview_gates(client, study):
    from copy import deepcopy
    prefix, tokens, upload = study
    left = {"type":"Polygon","coordinates":[[[73,20],[73.001,20],[73.001,20.001],[73,20.001],[73,20]]]}
    right = {"type":"Polygon","coordinates":[[[73.001,20],[73.002,20],[73.002,20.001],[73.001,20.001],[73.001,20]]]}
    _, lf = upload(left, "left"); _, rf = upload(right, "right")
    lp = baseline(client, prefix, tokens["admin"], lf); rp = baseline(client, prefix, tokens["admin"], rf)
    auth = headers(tokens["admin"])
    shifted = deepcopy(left)
    for coordinate in shifted["coordinates"][0]: coordinate[0] += .000001
    preview = client.post(prefix + "/geometry-drafts", headers=auth,
        json={"operation":"move","parcel_entity_ids":[lp["id"]],"draft_geometries":{lp["id"]:shifted}})
    assert preview.status_code == 200 and preview.json()["gates"]
    assert preview.json()["measurements"]["affected_neighbors"][0]["new_overlap_m2"] > 1
    rejected = client.post(prefix + "/geometry-changes", headers=auth,
        json={"operation":"move","parcel_entity_ids":[lp["id"]],"draft_geometries":{lp["id"]:shifted},"rationale":"Must not overlap neighbor"})
    assert rejected.status_code == 422
    both = {lp["id"]:deepcopy(left),rp["id"]:deepcopy(right)}
    for geometry in both.values():
        for coordinate in geometry["coordinates"][0]:
            if coordinate[0] == 73.001: coordinate[0] += .000001
    preview = client.post(prefix + "/geometry-drafts", headers=auth,
        json={"operation":"shared_edge","parcel_entity_ids":[lp["id"],rp["id"]],"draft_geometries":both})
    assert preview.status_code == 200 and preview.json()["gates"] == [], preview.text
    assert preview.json()["measurements"]["partition_difference_m2"] < .01
    stale = client.post(prefix + "/geometry-changes", headers=auth,
        json={"operation":"shared_edge","parcel_entity_ids":[lp["id"],rp["id"]],"draft_geometries":both,"expected_geometry_fingerprint":"0"*64,"rationale":"Stale preview"})
    assert stale.status_code == 409
    both[rp["id"]] = right  # moving only one side creates an overlap
    preview = client.post(prefix + "/geometry-drafts", headers=auth,
        json={"operation":"shared_edge","parcel_entity_ids":[lp["id"],rp["id"]],"draft_geometries":both})
    assert preview.status_code == 200 and preview.json()["gates"]


def test_control_invalid_crs_and_coordinates_are_rejected(client, study):
    prefix, tokens, upload = study
    dataset, _ = upload(RECTANGLE)
    auth = headers(tokens["admin"])
    for source_crs, target in (("EPSG:3857",[73,20]),("EPSG:4326",[73,99])):
        response = client.post(prefix + "/ground-control", headers=auth,
            json={"dataset_id":dataset["id"],"method":"translation","source_crs":source_crs,"target_crs":"EPSG:4326","control_points":[{"source":[73,20],"target":target}]})
        assert response.status_code == 422


def test_compliance_rejects_incompatible_nonfinite_and_invalid_ratio_inputs():
    from types import SimpleNamespace
    from app.advanced import compliance_result
    rule = SimpleNamespace(id="typed",version=1,effective_from=None,effective_to=None,category="setback",
        inputs=[{"name":"distance","units":"m2"}],formula={"actual":"distance"},threshold={"value":5,"units":"m"})
    assert compliance_result(rule,{"distance":3})["status"] == "insufficient_information"
    rule.inputs=[{"name":"distance","units":"m"}]
    assert compliance_result(rule,{"distance":float("nan")})["status"] == "insufficient_information"
    rule.category="far";rule.formula={};rule.inputs=[];rule.threshold={"value":2,"units":"ratio"}
    assert compliance_result(rule,{"floor_area_m2":"invalid","plot_area_m2":100})["status"] == "insufficient_information"
    assert compliance_result(rule,{"floor_area_m2":100,"plot_area_m2":-1})["status"] == "insufficient_information"


def test_two_band_raster_preview_encodes_real_rgb_rows(client, study):
    import struct
    import zlib
    import numpy as np
    from rasterio.io import MemoryFile
    from rasterio.transform import from_origin
    prefix, tokens, _ = study
    with MemoryFile() as memory:
        with memory.open(driver="GTiff",height=2,width=2,count=2,dtype="uint8",crs="EPSG:4326",transform=from_origin(73,20.002,.001,.001)) as source:
            source.write(np.array([[[1,2],[3,4]],[[4,3],[2,1]]],dtype="uint8"))
        raw = memory.read()
    asset = client.post(prefix + "/raster-assets",headers=headers(tokens["admin"]),files={"file":("two-band.tif",raw,"image/tiff")})
    assert asset.status_code == 201, asset.text
    response = client.get(prefix+f"/raster-assets/{asset.json()['id']}/preview",headers=headers(tokens["admin"]))
    assert response.status_code == 200
    data=response.content; assert data[:8] == b"\x89PNG\r\n\x1a\n"
    offset=8; compressed=b""
    while offset < len(data):
        length=struct.unpack(">I",data[offset:offset+4])[0]; kind=data[offset+4:offset+8]; payload=data[offset+8:offset+8+length]
        if kind==b"IHDR": assert payload[9] == 2  # RGB
        if kind==b"IDAT": compressed+=payload
        offset+=12+length
    assert len(zlib.decompress(compressed)) == 2*(1+2*3)
