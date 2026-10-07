"""Scoped read-only plans. Authorization precedes data access and pagination."""
import math
from datetime import date

from sqlalchemy import select
from shapely.geometry import Point, box, shape

from .advanced import parse_structured_query, resolve_canonical_geometry
from .auth import ensure_project_access, ensure_project_membership, project_member, utc_expired
from .models import ChangeProposal, Dataset, FieldAssignment, ParcelSourceLink, SourceFeature, TopologyConflict
from .services import analysis_crs_for, metric_geometry


def readable_dataset_ids(db, project_id, user):
    ensure_project_access(db, project_id, user)
    member = project_member(db, project_id, user.id)
    restricted = user.role == "admin" or (user.role in {"reviewer", "steward"} and member
                     and member.project_role in {"reviewer", "steward", "owner"})
    return {dataset.id for dataset in db.scalars(select(Dataset).where(Dataset.project_id == project_id))
            if dataset.access_classification != "restricted" or restricted}


def _ward(feature, dataset):
    values = {**(dataset.administrative_namespace or {}), **(feature.raw_attributes or {}),
              **(feature.canonical_attributes or {}), **(feature.administrative_context or {})}
    return str(values.get("ward", values.get("ward_code", ""))).casefold()


def _distance(geometry, longitude, latitude):
    if geometry.covers(Point(longitude, latitude)):
        return 0.0
    crs = analysis_crs_for(geometry)
    return metric_geometry(geometry, crs).distance(metric_geometry(Point(longitude, latitude), crs))


def execute_query(db, project_id, user, payload):
    ensure_project_membership(db, project_id, user)
    plan = parse_structured_query(payload.query)
    filters = plan["filters"]
    for field in ("ward", "date_from", "date_to", "bbox", "radius_m", "longitude", "latitude"):
        value = getattr(payload, field)
        if value is not None:
            filters[field] = value.isoformat() if isinstance(value, date) else value
    plan.update(limit=payload.limit, offset=payload.offset)
    if payload.date_from and payload.date_to and payload.date_from > payload.date_to:
        raise ValueError("date_from must not be after date_to")
    if payload.bbox and (not all(math.isfinite(v) for v in payload.bbox)
            or not (-180 <= payload.bbox[0] <= payload.bbox[2] <= 180)
            or not (-90 <= payload.bbox[1] <= payload.bbox[3] <= 90)):
        raise ValueError("bbox must be finite minx,miny,maxx,maxy in CRS84")
    if payload.radius_m is not None and (payload.longitude is None or payload.latitude is None):
        raise ValueError("longitude and latitude are required with radius_m")
    if plan["kind"] == "help":
        return {"plan": plan, "rows": [], "read_only": True,
                "clarification": "Ask for conflicts by ward, missing links, area, dated changes, or nearby assigned tasks."}

    nearby = plan["kind"] == "nearby_tasks"
    ensure_project_access(db, project_id, user, capability="fieldwork" if nearby else "departmental")
    # The citizen capability never authorizes departmental source queries. Its
    # dedicated records endpoint serves field-limited frozen publication data.
    datasets = {d.id: d for d in db.scalars(select(Dataset).where(Dataset.project_id == project_id))}
    visible = readable_dataset_ids(db, project_id, user) if not nearby else set()
    sources = {f.id: f for f in db.scalars(select(SourceFeature).join(Dataset).where(
                    Dataset.project_id == project_id, Dataset.id.in_(visible)))} if not nearby else {}
    spatial_filter = box(*payload.bbox) if payload.bbox else None
    requested_ward = str(filters.get("ward", "")).casefold()

    def eligible(features, geometry, timestamp, assigned_ward=None):
        if requested_ward and (assigned_ward != requested_ward if assigned_ward is not None else
                not any(_ward(f, datasets[f.dataset_id]) == requested_ward for f in features)):
            return False
        if payload.date_from or payload.date_to:
            if not timestamp:
                return False
            observed = str(timestamp)[:10]
            if payload.date_from and observed < payload.date_from.isoformat():
                return False
            if payload.date_to and observed > payload.date_to.isoformat():
                return False
        if spatial_filter is not None and (geometry is None or not geometry.intersects(spatial_filter)):
            return False
        if payload.radius_m is not None and (geometry is None or
                _distance(geometry, payload.longitude, payload.latitude) > payload.radius_m):
            return False
        return True

    rows = []
    if nearby:
        if payload.radius_m is None:
            return {"plan": plan, "rows": [], "read_only": True,
                    "clarification": "Supply longitude, latitude and radius in metres for nearby tasks."}
        assignments = select(FieldAssignment).where(FieldAssignment.project_id == project_id,
                    FieldAssignment.status.in_(["assigned", "active"])).order_by(FieldAssignment.id)
        if user.role == "field":
            assignments = assignments.where(FieldAssignment.assignee_id == user.id)
        for assignment in db.scalars(assignments):
            if utc_expired(assignment.expires_at):
                continue
            matching = []
            distances = []
            for parcel_id in assignment.parcel_entity_ids:
                geometry, _ = resolve_canonical_geometry(db, parcel_id)
                # Assigned references disclose geometry, not unrestricted source
                # attributes. Ward filtering is based on that bounded reference.
                ward = str((assignment.reference_policy or {}).get("ward", "")).casefold()
                if requested_ward and ward != requested_ward:
                    continue
                if eligible([], geometry, assignment.created_at, ward):
                    matching.append(parcel_id)
                    distances.append(_distance(geometry, payload.longitude, payload.latitude))
            if matching:
                rows.append({"id": assignment.id, "assignee_id": assignment.assignee_id,
                             "parcel_entity_ids": matching, "distance_m": min(distances),
                             "status": assignment.status, "created_at": assignment.created_at,
                             "expires_at": assignment.expires_at})
        rows.sort(key=lambda row: (row["distance_m"], row["id"]))
    elif plan["kind"] in {"missing_links", "area_threshold"}:
        linked = set(db.scalars(select(ParcelSourceLink.source_feature_id).where(ParcelSourceLink.project_id == project_id)))
        threshold = filters.get("area_threshold")
        if plan["kind"] == "area_threshold" and threshold is None:
            return {"plan": plan, "rows": [], "read_only": True, "clarification": plan.get("clarification")}
        for feature in sorted(sources.values(), key=lambda f: f.id):
            if feature.status != "processed" or (plan["kind"] == "missing_links" and feature.id in linked):
                continue
            dataset = datasets[feature.dataset_id]
            geometry = shape(feature.normalized_geometry) if feature.normalized_geometry else None
            if not eligible([feature], geometry, dataset.capture_date):
                continue
            row = {"feature_id": feature.id, "dataset_id": feature.dataset_id, "original_id": feature.original_id,
                   "capture_date": dataset.capture_date}
            if plan["kind"] == "area_threshold":
                values = {**feature.raw_attributes, **(feature.canonical_attributes or {})}
                raw = values.get("recorded_area", values.get("area", values.get("area_m2")))
                try:
                    value = float(raw)
                except (ValueError, TypeError):
                    continue
                if not math.isfinite(value) or value < threshold:
                    continue
                row.update(area=raw, units=values.get("area_units", "m2" if "area_m2" in values else None))
            rows.append(row)
    elif plan["kind"] == "conflicts":
        for conflict in db.scalars(select(TopologyConflict).where(TopologyConflict.project_id == project_id).order_by(TopologyConflict.id)):
            if conflict.dataset_id and conflict.dataset_id not in visible:
                continue
            if not conflict.dataset_id and visible != set(datasets):
                continue  # Unknown mixed-source aggregate is withheld.
            features = [sources[conflict.feature_id]] if conflict.feature_id in sources else []
            geometry = shape(features[0].normalized_geometry) if features and features[0].normalized_geometry else None
            if eligible(features, geometry, conflict.created_at):
                rows.append({"id": conflict.id, "type": conflict.conflict_type, "severity": conflict.severity,
                             "description": conflict.description, "details": conflict.details,
                             "created_at": conflict.created_at})
    elif plan["kind"] == "dated_changes":
        for change in db.scalars(select(ChangeProposal).where(ChangeProposal.project_id == project_id).order_by(ChangeProposal.id)):
            ids = [i for i in (change.source_feature_id, change.comparison_feature_id) if i]
            if not ids or any(i not in sources for i in ids):
                continue
            features = [sources[i] for i in ids]
            data = change.after_geometry or change.before_geometry
            if eligible(features, shape(data) if data else None, change.created_at):
                rows.append({"id": change.id, "type": change.change_type, "status": change.status,
                             "evidence": change.evidence, "created_at": change.created_at})
    total = len(rows)
    selected = rows[payload.offset:payload.offset + payload.limit]
    return {"plan": plan, "filters": filters, "rows": selected, "total": total, "offset": payload.offset,
            "read_only": True, "truncated": payload.offset + len(selected) < total}
