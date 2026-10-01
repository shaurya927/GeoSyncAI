"""Domain services: safe ingestion, explainable rules, validation and publication."""
import csv
import hashlib
import io
import json
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shapely.geometry import Point, shape, mapping, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform
from shapely.validation import explain_validity
from pyproj import CRS, Geod, Transformer

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import (AuditEvent, ChangeProposal, Dataset, Job, MatchProposal, ParcelEntity,
                     Project, ProjectMember, PublicationFeature, PublishedVersion, ReviewDecision,
                     SourceFeature, TopologyConflict, User, uid, utcnow)


ID_ALIASES = ("parcel_id", "plot_id", "khasra_no", "survey_number", "survey_no", "property_id", "id", "name")


def audit(db: Session, action: str, actor_id: str | None, project_id: str | None = None,
          target_type: str | None = None, target_id: str | None = None, details: dict[str, Any] | None = None) -> None:
    db.add(AuditEvent(action=action, actor_id=actor_id, project_id=project_id,
                      target_type=target_type, target_id=target_id, details=details or {}))


def safe_filename(name: str | None) -> str:
    name = Path(name or "upload.bin").name
    return "".join(c if c.isalnum() or c in ".-_" else "_" for c in name)[:180] or "upload.bin"


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _parse_geojson(data: bytes) -> tuple[list[dict[str, Any]], str | None]:
    doc = json.loads(data.decode("utf-8-sig"))
    if doc.get("type") == "FeatureCollection":
        crs = None
        if doc.get("crs"):
            crs_props = doc["crs"].get("properties", {})
            crs = crs_props.get("name") or crs_props.get("href")
        return [{"id": f.get("id"), "properties": f.get("properties") or {}, "geometry": f.get("geometry")} for f in doc.get("features", [])], crs
    if doc.get("type") == "Feature":
        return [{"id": doc.get("id"), "properties": doc.get("properties") or {}, "geometry": doc.get("geometry")}], None
    if "type" in doc and "coordinates" in doc:
        return [{"id": None, "properties": {}, "geometry": doc}], None
    raise ValueError("JSON is not GeoJSON")


def _parse_csv(data: bytes) -> tuple[list[dict[str, Any]], str | None]:
    text = data.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    features = []
    for row in rows:
        clean = {str(k): _jsonable(v) for k, v in row.items() if k is not None}
        geometry: dict[str, Any] | None = None
        for key in ("geometry", "geom", "geojson"):
            if clean.get(key):
                try:
                    geometry = json.loads(str(clean[key]))
                except json.JSONDecodeError:
                    pass
                break
        if geometry is None and clean.get("wkt"):
            try:
                from shapely import wkt
                geometry = mapping(wkt.loads(str(clean["wkt"])))
            except Exception:
                pass
        if geometry is None and clean.get("longitude") and clean.get("latitude"):
            geometry = mapping(Point(float(clean["longitude"]), float(clean["latitude"])))
        if geometry is None and clean.get("lon") and clean.get("lat"):
            geometry = mapping(Point(float(clean["lon"]), float(clean["lat"])))
        original_id = next((clean.get(k) for k in ID_ALIASES if clean.get(k)), None)
        features.append({"id": original_id, "properties": clean, "geometry": geometry})
    return features, None


def _fiona_crs(collection: Any) -> str | None:
    """Return a stable CRS label without treating Fiona metadata as authority."""
    try:
        if collection.crs_wkt:
            return CRS.from_wkt(collection.crs_wkt).to_string()
        if collection.crs:
            return CRS.from_user_input(collection.crs).to_string()
    except Exception:
        return None
    return None


def _parse_vector_with_fiona(path: Path, layer: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
    try:
        import fiona
    except ImportError as exc:
        raise ValueError("SHP ZIP and GeoPackage uploads require the Fiona dependency") from exc
    try:
        with fiona.open(path, layer=layer) as collection:
            crs = _fiona_crs(collection)
            features = []
            for item in collection:
                features.append({"id": item.get("id"), "properties": dict(item.get("properties") or {}),
                                 "geometry": item.get("geometry")})
            return features, crs
    except Exception as exc:
        raise ValueError(f"Could not read vector dataset: {exc}") from exc


def _parse_shapefile_zip(data: bytes) -> tuple[list[dict[str, Any]], str | None]:
    with tempfile.TemporaryDirectory(prefix="geosyncai-shp-") as temp_dir:
        root = Path(temp_dir)
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                members = archive.infolist()
                if any(member.is_dir() or Path(member.filename).is_absolute() or ".." in Path(member.filename).parts for member in members):
                    raise ValueError("Unsafe archive path; upload a flat SHP ZIP")
                archive.extractall(root)
        except zipfile.BadZipFile as exc:
            raise ValueError("Upload is not a valid ZIP archive") from exc
        shp_files = list(root.rglob("*.shp"))
        if len(shp_files) != 1:
            raise ValueError("SHP ZIP must contain exactly one .shp layer")
        shp = shp_files[0]
        stem = shp.with_suffix("")
        missing = [suffix for suffix in (".shx", ".dbf") if not stem.with_suffix(suffix).exists()]
        if missing:
            raise ValueError(f"SHP ZIP is missing required components: {', '.join(missing)}")
        return _parse_vector_with_fiona(shp)


def _parse_geopackage(data: bytes, filename: str | None) -> tuple[list[dict[str, Any]], str | None]:
    with tempfile.TemporaryDirectory(prefix="geosyncai-gpkg-") as temp_dir:
        path = Path(temp_dir) / safe_filename(filename or "upload.gpkg")
        path.write_bytes(data)
        try:
            import fiona
            layers = fiona.listlayers(path)
        except ImportError as exc:
            raise ValueError("GeoPackage uploads require the Fiona dependency") from exc
        except Exception as exc:
            raise ValueError(f"Could not inspect GeoPackage layers: {exc}") from exc
        if not layers:
            raise ValueError("GeoPackage contains no readable layers")
        return _parse_vector_with_fiona(path, layer=layers[0])


def parse_features(data: bytes, filename: str | None, mime_type: str | None) -> tuple[list[dict[str, Any]], str | None, str]:
    suffix = Path(filename or "").suffix.lower()
    if suffix in {".json", ".geojson"} or (mime_type and "json" in mime_type):
        features, crs = _parse_geojson(data)
        return features, crs, "geojson"
    if suffix == ".csv" or (mime_type and "csv" in mime_type):
        features, crs = _parse_csv(data)
        return features, crs, "csv"
    if suffix == ".zip":
        features, crs = _parse_shapefile_zip(data)
        return features, crs, "shp_zip"
    if suffix in {".gpkg", ".geopackage"}:
        features, crs = _parse_geopackage(data, filename)
        return features, crs, "geopackage"
    raise ValueError("Unsupported upload format; use GeoJSON, CSV, GeoPackage, or SHP ZIP")


def geometry_from_feature(feature: dict[str, Any]) -> BaseGeometry | None:
    try:
        return shape(feature["geometry"]) if feature.get("geometry") else None
    except Exception:
        return None


def normalize_geometry(geometry: BaseGeometry, declared_crs: str | None) -> tuple[BaseGeometry, str | None]:
    if not declared_crs:
        return geometry, None
    source = CRS.from_user_input(declared_crs)
    target = CRS.from_epsg(4326)
    if source == target:
        return geometry, "EPSG:4326"
    transformer = Transformer.from_crs(source, target, always_xy=True)
    return shapely_transform(transformer.transform, geometry), "EPSG:4326"


def spatial_column(geometry: BaseGeometry | None, normalized_crs: str | None) -> dict[str, Any]:
    if not geometry or normalized_crs != "EPSG:4326":
        return {}
    try:
        from .models import POSTGIS
        if POSTGIS:
            from geoalchemy2.shape import from_shape
            return {"spatial_geometry": from_shape(geometry, srid=4326)}
    except ImportError:
        pass
    return {}


def ingest_dataset(db: Session, dataset: Dataset, data: bytes, filename: str | None, mime_type: str | None) -> dict[str, Any]:
    settings = get_settings()
    digest = hashlib.sha256(data).hexdigest()
    stored_name = f"{dataset.id}-{safe_filename(filename)}"
    raw_path = settings.storage_path / stored_name
    raw_path.write_bytes(data)
    dataset.raw_path = str(raw_path)
    dataset.content_hash = digest
    dataset.original_filename = filename
    dataset.mime_type = mime_type
    report: dict[str, Any] = {"format": None, "errors": [], "warnings": [], "duplicate_ids": [],
                              "invalid_geometry": [], "empty_geometry": [], "attribute_only": 0,
                              "schema_fields": [], "processed": 0, "quarantined": 0}
    try:
        features, embedded_crs, file_format = parse_features(data, filename, mime_type)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        dataset.status = "rejected"
        report["errors"].append(str(exc))
        dataset.validation_report = report
        db.commit()
        raise ValueError(str(exc)) from exc
    report["format"] = file_format
    dataset.declared_crs = dataset.declared_crs or embedded_crs
    dataset.record_count = len(features)
    report["schema_fields"] = sorted({str(key) for feature in features for key in (feature.get("properties") or {}).keys()})
    has_geometry = any(item.get("geometry") for item in features)
    if not dataset.declared_crs and (file_format != "csv" or has_geometry):
        report["warnings"].append("CRS is unknown; spatial matching remains unverified")
    seen: set[str] = set()
    geometries: list[BaseGeometry] = []
    normalized_crs: str | None = None
    crs_error: str | None = None
    if dataset.declared_crs:
        try:
            CRS.from_user_input(dataset.declared_crs)
        except Exception as exc:
            crs_error = f"Invalid CRS definition: {dataset.declared_crs}"
            report["errors"].append(crs_error)
    for i, item in enumerate(features):
        original_id = str(item.get("id")) if item.get("id") is not None else None
        props = {str(k): _jsonable(v) for k, v in (item.get("properties") or {}).items()}
        if original_id is None:
            original_id = next((str(props[k]) for k in ID_ALIASES if props.get(k) not in (None, "")), None)
        original_geom = geometry_from_feature(item)
        geom = original_geom
        status = "processed"
        reason = None
        if original_id and original_id in seen:
            report["duplicate_ids"].append(original_id)
            report["warnings"].append(f"Duplicate identifier: {original_id}")
        if original_id:
            seen.add(original_id)
        if geom is None and file_format == "csv" and not has_geometry:
            # Revenue/register CSVs are valid attribute-only records. They retain
            # their original attributes and participate in identifier evidence,
            # while spatial evidence remains explicitly unavailable.
            report["attribute_only"] += 1
            reason = "Attribute-only record; spatial evidence unavailable"
        elif geom is None:
            report["empty_geometry"].append(i)
            status = "quarantined"
            reason = "Missing or unreadable geometry"
        elif geom.is_empty:
            report["empty_geometry"].append(i)
            status = "quarantined"
            reason = "Empty geometry"
        elif crs_error:
            status = "quarantined"
            reason = crs_error
        else:
            try:
                geom, feature_crs = normalize_geometry(geom, dataset.declared_crs)
                normalized_crs = feature_crs or normalized_crs
            except Exception as exc:
                status = "quarantined"
                reason = f"CRS transformation failed: {exc}"
                report["errors"].append(reason)
            geometries.append(geom)
            if not geom.is_valid:
                report["invalid_geometry"].append({"index": i, "reason": explain_validity(geom)})
                status = "quarantined"
                reason = explain_validity(geom)
            try:
                coords = list(geom.coords) if hasattr(geom, "coords") else []
                if any(abs(x) > 180 or abs(y) > 90 for x, y, *_ in coords):
                    report["warnings"].append(f"Coordinate range should be checked at feature {i}")
            except NotImplementedError:
                pass
        geometry_json = mapping(geom) if geom and not geom.is_empty and status == "processed" else None
        db.add(SourceFeature(dataset_id=dataset.id, original_id=original_id, raw_attributes=props,
                             original_geometry=mapping(original_geom) if original_geom and not original_geom.is_empty else None,
                             normalized_geometry=geometry_json, geometry_type=geom.geom_type if geom else None,
                             status=status, processing_reason=reason,
                             **spatial_column(geom if status == "processed" else None, normalized_crs)))
        if status == "processed":
            report["processed"] += 1
        else:
            report["quarantined"] += 1
    types = {g.geom_type for g in geometries}
    dataset.geometry_type = next(iter(types)) if len(types) == 1 else ("Mixed" if types else None)
    dataset.normalized_crs = normalized_crs
    dataset.normalized_count = report["processed"]
    dataset.status = "needs_crs_review" if crs_error or (not dataset.declared_crs and (file_format != "csv" or has_geometry)) else "processed"
    dataset.validation_report = report
    db.commit()
    return report


def feature_key(feature: SourceFeature, id_fields: list[str]) -> str | None:
    for key in id_fields or ID_ALIASES:
        value = feature.raw_attributes.get(key)
        if value not in (None, ""):
            return str(value).strip().casefold()
    return feature.original_id.strip().casefold() if feature.original_id else None


def geom_for(source: SourceFeature) -> BaseGeometry | None:
    try:
        return shape(source.normalized_geometry) if source.normalized_geometry else None
    except Exception:
        return None


def score_pair(left: SourceFeature, right: SourceFeature, id_fields: list[str], max_distance_m: float,
               allow_spatial: bool) -> tuple[float, dict[str, Any]]:
    lg, rg = geom_for(left), geom_for(right)
    overlap = 0.0
    distance = None
    area_difference = None
    if allow_spatial and lg and rg and not lg.is_empty and not rg.is_empty:
        union_area = lg.union(rg).area
        overlap = lg.intersection(rg).area / union_area if union_area else 0.0
        left_centroid, right_centroid = lg.centroid, rg.centroid
        _, _, distance = Geod(ellps="WGS84").inv(left_centroid.x, left_centroid.y, right_centroid.x, right_centroid.y)
        area_difference = abs(lg.area - rg.area) / max(lg.area, rg.area, 1e-12)
    left_key, right_key = feature_key(left, id_fields), feature_key(right, id_fields)
    identifier_agreement = bool(left_key and right_key and left_key == right_key)
    proximity = max(0.0, 1.0 - min((distance if distance is not None else max_distance_m) / max(max_distance_m, 0.001), 1.0))
    score = (0.55 * overlap) + (0.35 if identifier_agreement else 0.0) + (0.10 * proximity if allow_spatial else 0.0)
    evidence = {"intersection_over_union": round(overlap, 6), "centroid_distance_m": distance,
                "relative_area_difference": area_difference, "identifier_agreement": identifier_agreement,
                "left_key": left_key, "right_key": right_key,
                "spatial_evidence_used": allow_spatial,
                "explanation": ("exact namespace key plus available spatial evidence" if identifier_agreement and allow_spatial
                                else "exact namespace key; spatial evidence unavailable" if identifier_agreement
                                else "geometry proximity/overlap only")}
    return round(min(score, 1.0), 6), evidence


def run_matching(db: Session, project_id: str, left_dataset_id: str, right_dataset_id: str,
                 id_fields: list[str], max_distance: float, ambiguity_margin: float) -> dict[str, Any]:
    left_dataset, right_dataset = db.get(Dataset, left_dataset_id), db.get(Dataset, right_dataset_id)
    spatial_enabled = bool(left_dataset and right_dataset and left_dataset.normalized_crs == "EPSG:4326"
                           and right_dataset.normalized_crs == "EPSG:4326")
    left = list(db.scalars(select(SourceFeature).where(SourceFeature.dataset_id == left_dataset_id,
                                                       SourceFeature.status == "processed")))
    right = list(db.scalars(select(SourceFeature).where(SourceFeature.dataset_id == right_dataset_id,
                                                        SourceFeature.status == "processed")))
    result = {"matched": 0, "unmatched_left": 0, "ambiguous": 0,
              "spatial_evidence_used": spatial_enabled, "proposals": []}
    for lf in left:
        candidates: list[tuple[float, SourceFeature, dict[str, Any]]] = []
        for rf in right:
            score, evidence = score_pair(lf, rf, id_fields, max_distance, spatial_enabled)
            if evidence["identifier_agreement"] or (evidence["centroid_distance_m"] is not None
                                                      and evidence["centroid_distance_m"] <= max_distance) or evidence["intersection_over_union"] > 0:
                candidates.append((score, rf, evidence))
        candidates.sort(key=lambda x: x[0], reverse=True)
        if not candidates or candidates[0][0] < 0.35:
            result["unmatched_left"] += 1
            reason = "No candidate met the minimum rule score"
            if not spatial_enabled:
                reason += "; spatial evidence disabled because a normalized CRS is unavailable"
            proposal = MatchProposal(id=uid(), project_id=project_id, left_feature_id=lf.id, right_feature_id=None,
                                     score=0.0, status="unmatched", candidate_rank=None,
                                     evidence={"reason": reason, "alternatives": [], "spatial_evidence_used": spatial_enabled})
            db.add(proposal)
            result["proposals"].append({"id": proposal.id, "left_feature_id": lf.id, "status": "unmatched", "score": 0.0,
                                         "evidence": proposal.evidence})
            continue
        best = candidates[0]
        ambiguous = len(candidates) > 1 and (best[0] - candidates[1][0]) < ambiguity_margin
        status = "ambiguous" if ambiguous else "proposed"
        if ambiguous:
            result["ambiguous"] += 1
        else:
            result["matched"] += 1
        evidence = dict(best[2])
        evidence["alternatives"] = [{"feature_id": c[1].id, "score": c[0]} for c in candidates[1:3]]
        proposal = MatchProposal(id=uid(), project_id=project_id, left_feature_id=lf.id, right_feature_id=best[1].id,
                                 score=best[0], status=status, candidate_rank=1, evidence=evidence)
        db.add(proposal)
        result["proposals"].append({"id": proposal.id, "left_feature_id": lf.id, "right_feature_id": best[1].id,
                                     "status": status, "score": best[0], "evidence": evidence})
    db.commit()
    return result


def run_topology(db: Session, project_id: str, dataset_id: str) -> dict[str, int]:
    features = list(db.scalars(select(SourceFeature).where(SourceFeature.dataset_id == dataset_id)))
    counts: dict[str, int] = {}
    for f in features:
        geom = geom_for(f)
        if not geom:
            continue
        if not geom.is_valid:
            db.add(TopologyConflict(project_id=project_id, dataset_id=dataset_id, feature_id=f.id,
                                     conflict_type="invalid_geometry", severity="error",
                                     description=explain_validity(geom), details={}))
            counts["invalid_geometry"] = counts.get("invalid_geometry", 0) + 1
    for i, left in enumerate(features):
        lg = geom_for(left)
        if not lg or lg.is_empty:
            continue
        for right in features[i + 1:]:
            rg = geom_for(right)
            if rg and lg.overlaps(rg):
                db.add(TopologyConflict(project_id=project_id, dataset_id=dataset_id, feature_id=left.id,
                                         conflict_type="overlap", severity="warning",
                                         description="Features overlap; review coverage relationship",
                                         details={"other_feature_id": right.id, "overlap_area": lg.intersection(rg).area}))
                counts["overlap"] = counts.get("overlap", 0) + 1
    db.commit()
    return counts


def run_change_detection(db: Session, project_id: str, before_dataset_id: str, after_dataset_id: str,
                         id_fields: list[str], tolerance: float) -> dict[str, int]:
    before = list(db.scalars(select(SourceFeature).where(SourceFeature.dataset_id == before_dataset_id)))
    after = list(db.scalars(select(SourceFeature).where(SourceFeature.dataset_id == after_dataset_id)))
    before_by_key = {feature_key(f, id_fields): f for f in before if feature_key(f, id_fields)}
    after_by_key = {feature_key(f, id_fields): f for f in after if feature_key(f, id_fields)}
    counts: dict[str, int] = {}
    for key in sorted(set(before_by_key) | set(after_by_key)):
        old, new = before_by_key.get(key), after_by_key.get(key)
        area_delta = None
        spatial_details: dict[str, Any] = {}
        if old and not new:
            kind, boundary = "missing", False
        elif new and not old:
            kind, boundary = "added", False
        else:
            old_geom, new_geom = geom_for(old), geom_for(new)
            geometry_changed = bool(old_geom and new_geom and not old_geom.equals_exact(new_geom, tolerance))
            attrs_changed = old.raw_attributes != new.raw_attributes
            if not geometry_changed and not attrs_changed:
                continue
            kind, boundary = ("geometry_changed" if geometry_changed else "attribute_changed"), geometry_changed
            if geometry_changed and old_geom and new_geom:
                geod = Geod(ellps="WGS84")
                old_area = abs(geod.geometry_area_perimeter(old_geom)[0])
                new_area = abs(geod.geometry_area_perimeter(new_geom)[0])
                area_delta = new_area - old_area
                _, _, centroid_shift = geod.inv(old_geom.centroid.x, old_geom.centroid.y,
                                                 new_geom.centroid.x, new_geom.centroid.y)
                spatial_details = {"before_area_m2": old_area, "after_area_m2": new_area,
                                   "centroid_shift_m": centroid_shift,
                                   "hausdorff_distance_degrees": old_geom.hausdorff_distance(new_geom)}
        db.add(ChangeProposal(project_id=project_id, change_type=kind,
                              source_feature_id=new.id if new else old.id, comparison_feature_id=old.id if old and new else None,
                              before_geometry=old.normalized_geometry if old else None, after_geometry=new.normalized_geometry if new else None,
                              before_attributes=old.raw_attributes if old else None, after_attributes=new.raw_attributes if new else None,
                              area_delta=area_delta, boundary_change=boundary, status="proposed",
                              evidence={"namespace_key": key, "tolerance": tolerance,
                                        "timestamps_are_snapshot_dates": True, **spatial_details}))
        counts[kind] = counts.get(kind, 0) + 1
    db.commit()
    return counts


def next_version(db: Session, project_id: str) -> int:
    return (db.scalar(select(func.max(PublishedVersion.version_number)).where(PublishedVersion.project_id == project_id)) or 0) + 1


def validate_project(db: Session, project_id: str, actor_id: str) -> dict[str, Any]:
    open_errors = db.scalar(select(func.count(TopologyConflict.id)).where(TopologyConflict.project_id == project_id,
                                                                              TopologyConflict.severity == "error",
                                                                              TopologyConflict.status == "open")) or 0
    validated = 0
    failures: list[dict[str, str]] = []
    accepted = list(db.scalars(select(ChangeProposal).where(ChangeProposal.project_id == project_id,
                                                             ChangeProposal.status == "accepted")))
    for proposal in accepted:
        if proposal.boundary_change and proposal.after_geometry:
            try:
                geometry = shape(proposal.after_geometry)
                if geometry.is_empty or not geometry.is_valid:
                    failures.append({"change_id": proposal.id, "reason": explain_validity(geometry)})
                    continue
            except Exception as exc:
                failures.append({"change_id": proposal.id, "reason": f"Unreadable after geometry: {exc}"})
                continue
        proposal.status = "validated"
        validated += 1
    report = {"valid": not open_errors and not failures, "validated_changes": validated,
              "open_topology_errors": open_errors, "failures": failures}
    audit(db, "project_validated", actor_id, project_id, details=report)
    db.commit()
    return report


def publish(db: Session, project: Project, actor: User) -> PublishedVersion:
    unresolved_boundary = db.scalar(select(func.count(ChangeProposal.id)).where(ChangeProposal.project_id == project.id,
                                                                                ChangeProposal.boundary_change.is_(True),
                                                                                ChangeProposal.status != "validated")) or 0
    if unresolved_boundary:
        raise ValueError("Boundary changes require reviewer acceptance and successful validation before publication")
    unresolved = db.scalar(select(func.count(TopologyConflict.id)).where(TopologyConflict.project_id == project.id,
                                                                         TopologyConflict.severity == "error",
                                                                         TopologyConflict.status == "open")) or 0
    if unresolved:
        raise ValueError("Open topology errors block publication")
    version = PublishedVersion(project_id=project.id, version_number=next_version(db, project.id), created_by=actor.id,
                               lineage_manifest={"ruleset": "rules-v1", "published_at": utcnow().isoformat(), "features": []})
    db.add(version)
    db.flush()
    features = list(db.scalars(select(SourceFeature).join(Dataset, Dataset.id == SourceFeature.dataset_id)
                                .where(Dataset.project_id == project.id, SourceFeature.status == "processed")))
    seen: set[str] = set()
    for feature in features:
        # Publish only one copy of an exact source feature, preserving raw evidence.
        if feature.id in seen:
            continue
        seen.add(feature.id)
        entity = ParcelEntity(project_id=project.id, canonical_key=feature.original_id)
        db.add(entity)
        db.flush()
        applicable_changes = list(db.scalars(select(ChangeProposal).where(ChangeProposal.project_id == project.id,
                                                                           ChangeProposal.source_feature_id == feature.id,
                                                                           ChangeProposal.status == "validated")))
        change_ids = [change.id for change in applicable_changes]
        review_ids = list(db.scalars(select(ReviewDecision.id).where(ReviewDecision.project_id == project.id,
                                                                      ReviewDecision.target_type == "change",
                                                                      ReviewDecision.target_id.in_(change_ids)))) if change_ids else []
        published_geometry = feature.normalized_geometry
        published_attributes = feature.raw_attributes
        for change in applicable_changes:
            if change.after_geometry is not None:
                published_geometry = change.after_geometry
            if change.after_attributes is not None:
                published_attributes = change.after_attributes
        lineage = {"source_feature_id": feature.id, "dataset_id": feature.dataset_id,
                   "content_hash": db.get(Dataset, feature.dataset_id).content_hash,
                   "accepted_change_ids": change_ids, "review_decisions": review_ids}
        native = shape(published_geometry) if published_geometry else None
        db.add(PublicationFeature(version_id=version.id, source_feature_id=feature.id, parcel_entity_id=entity.id,
                                  attributes=published_attributes, geometry=published_geometry, lineage=lineage,
                                  **spatial_column(native, db.get(Dataset, feature.dataset_id).normalized_crs)))
        manifest_features = [*version.lineage_manifest["features"], lineage]
        version.lineage_manifest = {**version.lineage_manifest, "features": manifest_features}
    db.add(AuditEvent(project_id=project.id, actor_id=actor.id, action="published", target_type="published_version",
                      target_id=version.id, details={"version": version.version_number}))
    db.commit()
    return version


def bootstrap_synthetic(db: Session, project_id: str, actor_id: str, count: int = 25) -> dict[str, Any]:
    """Create a clearly labelled, deterministic demo pair; it is not cadastral evidence."""
    count = max(1, min(int(count), 250))
    datasets: list[Dataset] = []
    for name, shift in (("synthetic-reference", 0.0), ("synthetic-alternate", 0.00012)):
        collection = {"type": "FeatureCollection", "features": [], "crs": {"type": "name", "properties": {"name": "EPSG:4326"}}}
        for index in range(count):
            col, row = index % 10, index // 10
            x, y = col * 0.01 + shift, row * 0.01
            geometry = mapping(box(x, y, x + 0.008, y + 0.008))
            collection["features"].append({"type": "Feature", "id": f"P-{index + 1:04d}",
                                            "properties": {"parcel_id": f"P-{index + 1:04d}", "synthetic": True},
                                            "geometry": geometry})
        dataset = Dataset(project_id=project_id, name=name, source_organization="GeoSyncAI synthetic fixture",
                          declared_crs="EPSG:4326", metadata_json={"synthetic": True, "not_legal_evidence": True})
        db.add(dataset)
        db.flush()
        ingest_dataset(db, dataset, json.dumps(collection).encode(), f"{name}.geojson", "application/geo+json")
        datasets.append(dataset)
    audit(db, "synthetic_bootstrap", actor_id, project_id, details={"count": count, "dataset_ids": [d.id for d in datasets]})
    db.commit()
    return {"synthetic": True, "count": count, "dataset_ids": [d.id for d in datasets], "warning": "Synthetic data does not establish cadastral accuracy"}
