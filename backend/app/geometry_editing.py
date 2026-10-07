"""Preserve the original polygon boundaries; report checks in a common metric CRS."""
from shapely import union_all
from shapely.geometry import LineString, mapping, shape
from shapely.ops import split
from sqlalchemy import select

from .advanced import geometry_measurements, geometry_submission_gates, resolve_canonical_geometry
from .models import ParcelEntity, ParcelSourceLink, SourceFeature
from .querying import readable_dataset_ids
from .services import analysis_crs_for
from .spatial import check_coordinates
from .policy import processing_policy
from .publication import canonical_sha256


def preview_geometry(db, project_id, user, payload):
    entities = list(db.scalars(select(ParcelEntity).where(ParcelEntity.project_id == project_id,
                     ParcelEntity.status == "active", ParcelEntity.id.in_(payload.parcel_entity_ids))))
    if len(entities) != len(set(payload.parcel_entity_ids)):
        raise ValueError("All participants must be active parcels in this project")
    allowed = readable_dataset_ids(db, project_id, user)
    links = list(db.scalars(select(ParcelSourceLink).where(ParcelSourceLink.parcel_entity_id.in_(payload.parcel_entity_ids))))
    for link in links:
        source = db.get(SourceFeature, link.source_feature_id)
        if not source or source.dataset_id not in allowed:
            raise PermissionError("A participating parcel contains restricted source evidence")
    originals = {}
    for parcel_id in payload.parcel_entity_ids:
        geometry, _ = resolve_canonical_geometry(db, parcel_id)
        if geometry is None or geometry.geom_type not in {"Polygon", "MultiPolygon"}:
            raise ValueError("Each participant needs an approved polygon geometry")
        originals[parcel_id] = geometry
    if payload.before_geometries and (set(payload.before_geometries) != set(originals) or any(
            not originals[key].equals_exact(shape(payload.before_geometries[key]), 1e-10) for key in originals)):
        raise ValueError("Approved boundaries changed; reload participants before editing")
    if payload.operation == "split" and len(originals) != 1:
        raise ValueError("Choose exactly one parent to split")
    if payload.operation in {"merge", "shared_edge"} and len(originals) < 2:
        raise ValueError("Choose at least two participants")
    crs = analysis_crs_for(union_all(list(originals.values())))
    if payload.operation == "split":
        original = next(iter(originals.values()))
        if payload.cut_line:
            cut = shape(payload.cut_line)
            check_coordinates(cut, True)
            if cut.geom_type != "LineString" or not cut.is_simple or cut.is_empty:
                raise ValueError("Draw a simple cut line crossing the parcel")
            cutter = cut
        else:
            minx, miny, maxx, maxy = original.bounds
            padding = max(maxx-minx, maxy-miny, 0.0001)
            cutter = LineString([((minx+maxx)/2, miny-padding), ((minx+maxx)/2, maxy+padding)])
        parts = [part for part in split(original, cutter).geoms if not part.is_empty and part.area > 0]
        if len(parts) < 2:
            raise ValueError("The cut does not split the parcel; extend it across the boundary")
        output = {f"child-{i+1}": mapping(part) for i, part in enumerate(parts)}
    elif payload.operation == "merge":
        merged = union_all(list(originals.values()))
        if not merged.is_valid or merged.geom_type not in {"Polygon", "MultiPolygon"}:
            raise ValueError("The selected parcels do not form a valid polygon union")
        from .services import metric_geometry
        overlap = sum(metric_geometry(g, crs).area for g in originals.values()) - metric_geometry(merged, crs).area
        if overlap > processing_policy(db, project_id)["overlap_tolerance_m2"]:
            raise ValueError("Resolve overlapping participants before merging their identities")
        output = {"merged": mapping(merged)}
    else:
        output = payload.draft_geometries or {key: mapping(g) for key, g in originals.items()}
        if set(output) != set(originals):
            raise ValueError("Draft keys must exactly match selected participant identities")
    measured = geometry_measurements(db, payload.parcel_entity_ids, output)
    return {"draft_geometries": output, "measurements": measured,
            "gates": geometry_submission_gates(payload.operation, measured, processing_policy(db, project_id)),
            "before_fingerprint": canonical_sha256(measured["before_geometries"]),
            "cut_line": payload.cut_line, "analysis_crs": crs,
            "requires_review": True, "original_geometries": {key: mapping(g) for key, g in originals.items()}}
