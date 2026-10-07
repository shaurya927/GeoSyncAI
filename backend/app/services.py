"""Domain services: safe ingestion, explainable rules, validation and publication."""
import csv
import hashlib
import io
import json
import math
import tempfile
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

from shapely.geometry import Point, shape, mapping, box
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree
from shapely.validation import explain_validity
from pyproj import CRS

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .config import get_settings
from .policy import processing_policy
from .spatial import check_coordinates, reproject, transformation_metadata
from .models import (AuditEvent, ChangeProposal, Dataset, MatchProposal, ModelArtifact, ParcelEntity,
                     ParcelSourceLink, Project, PublishedVersion, SourceFeature, TopologyConflict, uid, utcnow)


ID_ALIASES = ("parcel_id", "plot_id", "khasra_no", "survey_number", "survey_no", "property_id", "id", "name")
NAMESPACE_ALIASES = (
    "village", "village_name", "village_code", "gram", "gram_panchayat", "ward", "ward_name",
    "district", "district_name", "taluka", "tehsil", "block", "mandal", "municipality",
    "administrative_unit", "admin_code", "admin_name",
)
NAMESPACE_GROUPS = {
    "village": ("village", "village_name", "gram", "gram_panchayat"),
    "village_code": ("village_code", "admin_code", "village_id"),
    "ward": ("ward", "ward_name"),
    "district": ("district", "district_name"),
    "subdistrict": ("taluka", "tehsil", "block", "mandal"),
    "administrative_unit": ("municipality", "administrative_unit", "admin_name"),
}


def normalize_identifier(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return " ".join(str(value).strip().casefold().split())


def administrative_context(attributes: dict[str, Any], fields: list[str] | None = None) -> dict[str, str]:
    """Extract only explicit administrative fields; a parcel number is never a namespace."""
    result: dict[str, str] = {}
    lowered = {str(key).casefold(): value for key, value in attributes.items()}
    if fields:
        selected = {field.casefold() for field in fields}
        groups = {name: aliases for name, aliases in NAMESPACE_GROUPS.items()
                  if selected.intersection(aliases)}
        for field in selected - {alias for aliases in groups.values() for alias in aliases}:
            groups[field] = (field,)
    else:
        groups = NAMESPACE_GROUPS
    for name, aliases in groups.items():
        value = next((normalize_identifier(lowered.get(alias)) for alias in aliases
                      if normalize_identifier(lowered.get(alias))), None)
        if value:
            result[name] = value
    return result


def namespace_compatible(left: SourceFeature, right: SourceFeature) -> bool:
    left_context = left.administrative_context or administrative_context(left.raw_attributes)
    right_context = right.administrative_context or administrative_context(right.raw_attributes)
    if not left_context or not right_context:
        return True
    common = set(left_context).intersection(right_context)
    return not common or all(left_context[key] == right_context[key] for key in common)


def touch_project(db: Session, project_id: str) -> Project | None:
    db.execute(update(Project).where(Project.id == project_id)
               .values(workflow_revision=Project.workflow_revision + 1, validated_revision=None)
               .execution_options(synchronize_session="fetch"))
    project = db.get(Project, project_id)
    return project


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
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)


def _parse_geojson(data: bytes) -> tuple[list[dict[str, Any]], str | None]:
    def invalid_constant(value):
        raise ValueError(f"Nonfinite JSON number: {value}")
    doc = json.loads(data.decode("utf-8-sig"), parse_constant=invalid_constant)
    if not isinstance(doc, dict):
        raise ValueError("GeoJSON document must be an object")
    if doc.get("type") == "FeatureCollection":
        if not isinstance(doc.get("features"), list) or any(not isinstance(f, dict) for f in doc['features']):
            raise ValueError("GeoJSON features must be a list of objects")
        if len(doc["features"]) > get_settings().max_source_records:
            raise ValueError("Dataset exceeds the configured record limit")
        if any(f.get('properties') is not None and not isinstance(f.get('properties'), dict) for f in doc['features']):
            raise ValueError("Feature properties must be objects or null")
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
    rows = csv.DictReader(io.StringIO(text))
    features = []
    for row in rows:
        if len(features) >= get_settings().max_source_records:
            raise ValueError("Dataset exceeds the configured record limit")
        if None in row:
            raise ValueError("CSV record contains more columns than its header")
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
        try:
            if geometry is None and clean.get("longitude") and clean.get("latitude"):
                geometry = mapping(Point(float(clean["longitude"]), float(clean["latitude"])))
            if geometry is None and clean.get("lon") and clean.get("lat"):
                geometry = mapping(Point(float(clean["lon"]), float(clean["lat"])))
        except (ValueError, TypeError):
            geometry = None
        original_id = next((clean.get(k) for k in ID_ALIASES if clean.get(k)), None)
        features.append({"id": original_id, "properties": clean, "geometry": geometry,
                         "geometry_expected": any(clean.get(k) for k in ('geometry','geom','geojson','wkt','longitude','latitude','lon','lat'))})
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
                if len(features) >= get_settings().max_source_records:
                    raise ValueError("Dataset exceeds the configured record limit")
                features.append({"id": item.get("id"), "properties": dict(item.get("properties") or {}),
                                 "geometry": item.get("geometry")})
            return features, crs
    except Exception as exc:
        raise ValueError("Could not read vector dataset. Check its format and required companion files.") from exc


def _parse_shapefile_zip(data: bytes) -> tuple[list[dict[str, Any]], str | None]:
    with tempfile.TemporaryDirectory(prefix="geosyncai-shp-") as temp_dir:
        root = Path(temp_dir)
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                members = archive.infolist()
                settings = get_settings()
                if len(members) > settings.max_zip_members:
                    raise ValueError(f"Archive contains too many members (limit {settings.max_zip_members})")
                if sum(member.file_size for member in members) > settings.max_zip_uncompressed_bytes:
                    raise ValueError("Archive exceeds the configured expanded-size limit")
                if any(member.is_dir() or Path(member.filename).is_absolute() or ".." in Path(member.filename).parts
                       or "\\" in member.filename or ":" in member.filename
                       or (member.external_attr >> 16) & 0o170000 == 0o120000
                       or member.flag_bits & 1
                       or member.file_size > settings.max_upload_bytes
                       or member.file_size > max(1, member.compress_size) * settings.max_zip_compression_ratio
                       or Path(member.filename).suffix.lower() not in {'.shp', '.shx', '.dbf', '.prj', '.cpg', '.qix', '.sbn', '.sbx', '.xml'}
                       for member in members):
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
            raise ValueError("Could not inspect GeoPackage layers. Check the file format and layer structure.") from exc
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
        raise ValueError("Source CRS is unresolved")
    transformation_metadata(declared_crs)
    return reproject(geometry, declared_crs, "EPSG:4326"), "EPSG:4326"


def analysis_crs_for(geometry: BaseGeometry | None) -> str | None:
    """Choose a local UTM CRS for metric work while retaining EPSG:4326 for display."""
    if not geometry or geometry.is_empty:
        return None
    centroid = geometry.centroid
    if not -80 <= centroid.y <= 84:
        raise ValueError("Automatic UTM analysis is limited to latitudes -80 through 84")
    zone = max(1, min(60, int((centroid.x + 180) // 6) + 1))
    epsg = (32600 if centroid.y >= 0 else 32700) + zone
    return f"EPSG:{epsg}"


def metric_geometry(geometry: BaseGeometry | None, analysis_crs: str | None) -> BaseGeometry | None:
    if not geometry:
        return None
    if not analysis_crs or not CRS(analysis_crs).is_projected:
        raise ValueError("A projected analysis CRS is required for metric operations")
    return reproject(geometry, "EPSG:4326", analysis_crs)


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
    with raw_path.open("xb") as stream:
        stream.write(data)
    dataset.raw_path = str(raw_path)
    dataset.content_hash = digest
    dataset.original_filename = filename
    dataset.mime_type = mime_type
    report: dict[str, Any] = {"format": None, "errors": [], "warnings": [], "duplicate_ids": [],
                              "invalid_geometry": [], "empty_geometry": [], "attribute_only": 0,
                              "schema_fields": [], "processed": 0, "quarantined": 0,
                              "has_geometry": False, "spatial_records": 0}
    try:
        features, embedded_crs, file_format = parse_features(data, filename, mime_type)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        dataset.status = "rejected"
        report["errors"].append("Source parsing failed. Check format, record limits and archive safety.")
        dataset.validation_report = report
        db.commit()
        raise ValueError("Source parsing failed. Check format, record limits and archive safety.") from exc
    report["format"] = file_format
    dataset.declared_crs = dataset.declared_crs or embedded_crs
    dataset.record_count = len(features)
    report["schema_fields"] = sorted({str(key) for feature in features for key in (feature.get("properties") or {}).keys()})
    report['schema_summary'] = [{
        'name': field,
        'types': sorted({type((feature.get('properties') or {}).get(field)).__name__ for feature in features}),
        'sample_values': [str((feature.get('properties') or {}).get(field))[:80] for feature in features[:3]]
                         if field.casefold() in (*ID_ALIASES, *NAMESPACE_ALIASES, 'area_units', 'recorded_area') else [],
    } for field in report['schema_fields']]
    has_geometry = any(item.get("geometry") or item.get("geometry_expected") for item in features)
    report["has_geometry"] = has_geometry
    report["spatial_records"] = sum(1 for item in features if item.get("geometry"))
    if not dataset.declared_crs and (file_format != "csv" or has_geometry):
        report["warnings"].append("CRS is unknown; spatial matching remains unverified")
    seen: set[str] = set()
    geometries: list[BaseGeometry] = []
    normalized_crs: str | None = None
    crs_error: str | None = None
    if dataset.declared_crs:
        try:
            CRS.from_user_input(dataset.declared_crs)
        except Exception:
            crs_error = "Invalid CRS definition. Confirm the source coordinate reference system."
            report["errors"].append(crs_error)
    for i, item in enumerate(features):
        original_id = str(item.get("id")) if item.get("id") is not None else None
        props = {str(k): _jsonable(v) for k, v in (item.get("properties") or {}).items()}
        if original_id is None:
            original_id = next((str(props[k]) for k in ID_ALIASES if props.get(k) not in (None, "")), None)
        original_geom = geometry_from_feature(item)
        coordinate_error = None
        if original_geom is not None and not original_geom.is_empty:
            try:
                check_coordinates(original_geom)
            except ValueError:
                coordinate_error = "Invalid source coordinates. Check geometry type, finite values and coordinate ranges."
                original_geom = None  # exact malformed representation remains in immutable raw bytes
        geom = original_geom
        status = "processed"
        reason = None
        context = administrative_context(props)
        if original_id and original_id in seen:
            report["duplicate_ids"].append(original_id)
            report["warnings"].append(f"Duplicate identifier: {original_id}")
        if original_id:
            seen.add(original_id)
        if coordinate_error:
            status, reason = 'quarantined', coordinate_error
            report['errors'].append(reason)
        elif geom is None and file_format == "csv" and not item.get("geometry_expected"):
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
        elif not dataset.declared_crs:
            # Keep the source geometry for a later explicit CRS confirmation, but do
            # not expose it as normalized spatial evidence or publishable geometry.
            status = "awaiting_crs"
            reason = "Spatial record is awaiting explicit CRS confirmation"
        elif crs_error:
            status = "quarantined"
            reason = crs_error
        else:
            try:
                geom, feature_crs = normalize_geometry(geom, dataset.declared_crs)
                normalized_crs = feature_crs or normalized_crs
                geometries.append(geom)
            except Exception:
                status = "quarantined"
                reason = "CRS transformation failed. Check source CRS and coordinate ranges."
                report["errors"].append(reason)
            if status == "processed" and not geom.is_valid:
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
                             administrative_context=context,
                             status=status, processing_reason=reason,
                             **spatial_column(geom if status == "processed" else None, normalized_crs)))
        if status == "processed":
            report["processed"] += 1
        else:
            report["quarantined"] += 1
    types = {g.geom_type for g in geometries}
    dataset.geometry_type = next(iter(types)) if len(types) == 1 else ("Mixed" if types else None)
    dataset.normalized_crs = normalized_crs
    dataset.analysis_crs = analysis_crs_for(geometries[0]) if geometries else None
    dataset.crs_transform = ({**transformation_metadata(dataset.declared_crs),
                              "analysis_crs": dataset.analysis_crs,
                              "accuracy_metadata": dataset.accuracy_metadata}
                             if normalized_crs else None)
    dataset.normalized_count = report["processed"]
    dataset.status = "needs_crs_review" if crs_error or (not dataset.declared_crs and (file_format != "csv" or has_geometry)) else "processed"
    dataset.validation_report = report
    touch_project(db, dataset.project_id)
    db.commit()
    return report


def confirm_dataset_crs(db: Session, dataset: Dataset, crs: str, actor_id: str, reason: str) -> dict[str, Any]:
    """Reprocess retained source geometries after an explicit officer CRS decision."""
    if not dataset.raw_path or hashlib.sha256(Path(dataset.raw_path).read_bytes()).hexdigest() != dataset.content_hash:
        raise ValueError("Original upload hash verification failed")
    try:
        CRS.from_user_input(crs)
    except Exception as exc:
        raise ValueError(f"Invalid CRS definition: {crs}") from exc

    features = list(db.scalars(select(SourceFeature).where(SourceFeature.dataset_id == dataset.id)))
    # Fail atomically if the officer's definition cannot plausibly transform
    # the retained coordinates. Invalid topology remains inspectable quarantine.
    for feature in features:
        if feature.original_geometry:
            normalize_geometry(shape(feature.original_geometry), crs)
    invalidate_dataset_evidence(db, dataset.id)
    report = dict(dataset.validation_report or {})
    report.update({"errors": [], "warnings": [], "invalid_geometry": [], "empty_geometry": [],
                   "processed": 0, "quarantined": 0, "attribute_only": 0, "crs_confirmation_reason": reason})
    normalized_crs = "EPSG:4326"
    geometries: list[BaseGeometry] = []
    for index, feature in enumerate(features):
        raw_geometry = None
        try:
            raw_geometry = shape(feature.original_geometry) if feature.original_geometry else None
        except Exception:
            raw_geometry = None
        feature.normalized_geometry = None
        if hasattr(feature, "spatial_geometry"):
            feature.spatial_geometry = None
        feature.status = "processed"
        feature.processing_reason = None
        if raw_geometry is None:
            if dataset.validation_report and dataset.validation_report.get("format") == "csv" and not any(
                feature.raw_attributes.get(k) for k in ('geometry','geom','geojson','wkt','longitude','latitude','lon','lat')):
                report["attribute_only"] = report.get("attribute_only", 0) + 1
                report["processed"] += 1
            else:
                feature.status = "quarantined"
                feature.processing_reason = "Missing or unreadable geometry"
                report["empty_geometry"].append(index)
                report["quarantined"] += 1
            continue
        if raw_geometry.is_empty:
            feature.status = "quarantined"
            feature.processing_reason = "Empty geometry"
            report["empty_geometry"].append(index)
            report["quarantined"] += 1
            continue
        try:
            normalized, _ = normalize_geometry(raw_geometry, crs)
            if not normalized.is_valid:
                feature.status = "quarantined"
                feature.processing_reason = explain_validity(normalized)
                report["invalid_geometry"].append({"index": index, "reason": feature.processing_reason})
                report["quarantined"] += 1
                continue
            feature.normalized_geometry = mapping(normalized)
            feature.geometry_type = normalized.geom_type
            feature.administrative_context = feature.administrative_context or administrative_context(feature.raw_attributes)
            if hasattr(feature, "spatial_geometry"):
                feature.spatial_geometry = spatial_column(normalized, normalized_crs).get("spatial_geometry")
            geometries.append(normalized)
            report["processed"] += 1
        except Exception:
            feature.status = "quarantined"
            feature.processing_reason = "CRS transformation failed. Check source CRS and coordinate ranges."
            report["errors"].append(feature.processing_reason)
            report["quarantined"] += 1

    dataset.declared_crs = crs
    dataset.normalized_crs = normalized_crs if geometries else None
    dataset.analysis_crs = analysis_crs_for(geometries[0]) if geometries else None
    dataset.crs_transform = {**transformation_metadata(crs),
                             "analysis_crs": dataset.analysis_crs,
                             "confirmed_reason": reason}
    dataset.geometry_type = (next(iter({geometry.geom_type for geometry in geometries}))
                             if len({geometry.geom_type for geometry in geometries}) == 1
                             else "Mixed" if geometries else None)
    dataset.normalized_count = report["processed"]
    dataset.status = "processed"
    dataset.crs_confirmed_by = actor_id
    dataset.crs_confirmed_at = utcnow()
    dataset.validation_report = report
    touch_project(db, dataset.project_id)
    audit(db, "dataset_crs_confirmed", actor_id, dataset.project_id, "dataset", dataset.id,
          {"source_crs": crs, "analysis_crs": dataset.analysis_crs, "reason": reason})
    db.commit()
    return report


def invalidate_dataset_evidence(db: Session, dataset_id: str):
    ids = list(db.scalars(select(SourceFeature.id).where(SourceFeature.dataset_id == dataset_id)))
    if db.scalar(select(ParcelSourceLink.id).where(ParcelSourceLink.source_feature_id.in_(ids))):
        raise ValueError("Dataset has reviewed canonical links; upload a new dataset version instead of changing reviewed evidence")
    for model, condition in [
        (MatchProposal, MatchProposal.left_feature_id.in_(ids) | MatchProposal.right_feature_id.in_(ids)),
        (ChangeProposal, ChangeProposal.source_feature_id.in_(ids) | ChangeProposal.comparison_feature_id.in_(ids)),
        (TopologyConflict, TopologyConflict.dataset_id == dataset_id),
    ]:
        db.execute(update(model).where(condition).values(status="superseded", revision=model.revision + 1))


def identifier_value(feature: SourceFeature, id_fields: list[str]) -> str | None:
    if feature.canonical_attributes:
        for semantic in ("parcel_id", "survey_number", "property_account"):
            value = normalize_identifier(feature.canonical_attributes.get(semantic))
            if value:
                return f"{semantic}:{value}"
        return None
    lowered = {key.casefold(): value for key, value in feature.raw_attributes.items()}
    for key in id_fields or ID_ALIASES:
        value = normalize_identifier(lowered.get(key.casefold()))
        if value:
            return value
    return normalize_identifier(feature.original_id)


def feature_key(feature: SourceFeature, id_fields: list[str]) -> str | None:
    identifier = identifier_value(feature, id_fields)
    if not identifier:
        return None
    context = feature.administrative_context or administrative_context(feature.raw_attributes)
    namespace = "|".join(f"{key}={context[key]}" for key in sorted(context))
    return f"{namespace}|id={identifier}" if namespace else f"id={identifier}"


def geom_for(source: SourceFeature) -> BaseGeometry | None:
    try:
        return shape(source.normalized_geometry) if source.normalized_geometry else None
    except Exception:
        return None


def score_pair(left: SourceFeature, right: SourceFeature, id_fields: list[str], max_distance_m: float,
               allow_spatial: bool, namespace_fields: list[str] | None = None,
               analysis_crs: str | None = None) -> tuple[float, dict[str, Any]]:
    lg, rg = geom_for(left), geom_for(right)
    overlap = 0.0
    distance = None
    area_difference = None
    boundary_distance = None
    if allow_spatial and lg and rg and not lg.is_empty and not rg.is_empty:
        analysis_crs = analysis_crs or analysis_crs_for(lg)
        lg, rg = metric_geometry(lg, analysis_crs), metric_geometry(rg, analysis_crs)
        union_area = lg.union(rg).area
        overlap = lg.intersection(rg).area / union_area if union_area else 0.0
        left_centroid, right_centroid = lg.centroid, rg.centroid
        distance = left_centroid.distance(right_centroid)
        boundary_distance = lg.hausdorff_distance(rg)
        area_difference = abs(lg.area - rg.area) / max(lg.area, rg.area, 1e-12)
    left_identifier, right_identifier = identifier_value(left, id_fields), identifier_value(right, id_fields)
    left_namespace = administrative_context(left.raw_attributes, namespace_fields) if namespace_fields else (left.administrative_context or administrative_context(left.raw_attributes))
    right_namespace = administrative_context(right.raw_attributes, namespace_fields) if namespace_fields else (right.administrative_context or administrative_context(right.raw_attributes))
    common_context = set(left_namespace).intersection(right_namespace)
    namespace_match = all(left_namespace[key] == right_namespace[key] for key in common_context)
    identifier_agreement = bool(left_identifier and right_identifier and left_identifier == right_identifier and namespace_match)
    proximity = max(0.0, 1.0 - min((distance if distance is not None else max_distance_m) / max(max_distance_m, 0.001), 1.0))
    score = (0.55 * overlap) + (0.35 if identifier_agreement else 0.0) + (0.10 * proximity if allow_spatial else 0.0)
    evidence = {"intersection_over_union": round(overlap, 6), "centroid_distance_m": distance,
                "relative_area_difference": area_difference, "identifier_agreement": identifier_agreement,
                "left_key": feature_key(left, id_fields), "right_key": feature_key(right, id_fields),
                "left_identifier": left_identifier, "right_identifier": right_identifier,
                "left_namespace": left_namespace, "right_namespace": right_namespace,
                "namespace_compatible": namespace_match,
                "namespace_verified": bool(common_context and left.canonical_attributes and right.canonical_attributes),
                "analysis_crs": analysis_crs, "boundary_distance_m": boundary_distance,
                "method": "rules-v2", "max_distance_m": max_distance_m,
                "spatial_evidence_used": allow_spatial,
                "explanation": ("exact namespace key plus available spatial evidence" if identifier_agreement and allow_spatial
                                else "exact namespace key; spatial evidence unavailable" if identifier_agreement
                                else "geometry proximity/overlap only")}
    return round(min(score, 1.0), 6), evidence


def run_matching(db: Session, project_id: str, left_dataset_id: str, right_dataset_id: str,
                 id_fields: list[str], max_distance: float, ambiguity_margin: float,
                 namespace_fields: list[str] | None = None) -> dict[str, Any]:
    left_dataset, right_dataset = db.get(Dataset, left_dataset_id), db.get(Dataset, right_dataset_id)
    spatial_enabled = bool(left_dataset and right_dataset and left_dataset.normalized_crs == "EPSG:4326"
                           and right_dataset.normalized_crs == "EPSG:4326")
    left = list(db.scalars(select(SourceFeature).where(SourceFeature.dataset_id == left_dataset_id,
                                                       SourceFeature.status == "processed").order_by(SourceFeature.original_id, SourceFeature.id)))
    right = list(db.scalars(select(SourceFeature).where(SourceFeature.dataset_id == right_dataset_id,
                                                         SourceFeature.status == "processed")))
    analysis_crs = left_dataset.analysis_crs or right_dataset.analysis_crs
    right_spatial = [(feature, metric_geometry(geom_for(feature), analysis_crs)) for feature in right if geom_for(feature)]
    right_tree = STRtree([geometry for _, geometry in right_spatial]) if right_spatial and spatial_enabled else None
    result = {"matched": 0, "unmatched_left": 0, "ambiguous": 0,
              "spatial_evidence_used": spatial_enabled, "proposals": []}
    active_model = db.scalar(select(ModelArtifact).where(ModelArtifact.project_id == project_id,
                                                         ModelArtifact.model_type == "supervised_logistic_ranker",
                                                         ModelArtifact.activation_status == "active")
                             .order_by(ModelArtifact.created_at.desc()))
    if active_model:
        result["ranking_model"] = {"version": active_model.version, "score_type": "uncalibrated_model_score",
                                    "probability_claim": False, "dataset_fingerprint": active_model.dataset_fingerprint}
    proposals_by_right: dict[str, list[tuple[MatchProposal, dict[str, Any]]]] = {}
    for lf in left:
        candidates: list[tuple[float, SourceFeature, dict[str, Any]]] = []
        left_geometry = geom_for(lf)
        if right_tree is not None and left_geometry is not None:
            candidate_indexes = right_tree.query(metric_geometry(left_geometry, analysis_crs), predicate="dwithin", distance=max_distance)
            candidate_right = [right_spatial[int(index)][0] for index in candidate_indexes]
        else:
            candidate_right = right if not spatial_enabled else []
        for rf in candidate_right:
            score, evidence = score_pair(lf, rf, id_fields, max_distance, spatial_enabled, namespace_fields, analysis_crs)
            if active_model:
                artifact = active_model.artifact or {}
                fields = artifact.get("fields", [])
                weights = artifact.get("weights", [])
                bias = float(artifact.get("bias", 0.0))
                vector = {"score": float(evidence.get("score", 0.0)),
                          "identifier_agreement": float(bool(evidence.get("identifier_agreement"))),
                          "iou": float(evidence.get("intersection_over_union", 0.0) or 0.0),
                          "distance_inverse": 1.0 / (1.0 + float(evidence.get("centroid_distance_m") or 100000.0)),
                          "namespace_compatible": float(bool(evidence.get("namespace_compatible", True)))}
                logits = bias + sum(float(weight) * float(vector.get(field, 0.0)) for field, weight in zip(fields, weights))
                model_score = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, logits))))
                evidence["deterministic_score"] = score
                evidence["learned_score"] = round(model_score, 6)
                score = round(model_score, 6)
            if namespace_fields:
                if not evidence["namespace_compatible"]:
                    continue
            spatial_candidate = ((evidence["centroid_distance_m"] is not None
                                  and evidence["centroid_distance_m"] <= max_distance)
                                 or evidence["intersection_over_union"] > 0)
            # An identifier is evidence, never a licence to match parcels that
            # are spatially incompatible when spatial evidence is available.
            if evidence["namespace_compatible"] and (spatial_candidate or (not spatial_enabled and evidence["identifier_agreement"] and evidence["namespace_verified"])):
                candidates.append((score, rf, evidence))
        candidates.sort(key=lambda x: (-x[0], x[1].original_id or '', x[1].id))
        available_candidates = candidates
        if not available_candidates or available_candidates[0][0] < 0.35:
            result["unmatched_left"] += 1
            reason = ("Candidate is already assigned by a stronger one-to-one proposal"
                      if candidates and not available_candidates else "No candidate met the minimum rule score")
            if not spatial_enabled:
                reason += "; spatial evidence disabled because a normalized CRS is unavailable"
            proposal = MatchProposal(id=uid(), project_id=project_id, left_feature_id=lf.id, right_feature_id=None,
                                     score=0.0, status="unmatched", candidate_rank=None,
                                     evidence={"reason": reason, "alternatives": [], "spatial_evidence_used": spatial_enabled})
            db.add(proposal)
            result["proposals"].append({"id": proposal.id, "left_feature_id": lf.id, "status": "unmatched", "score": 0.0,
                                         "evidence": proposal.evidence})
            continue
        candidates = available_candidates
        best = candidates[0]
        possible_split = sum(c[2]["intersection_over_union"] > .2 for c in candidates) > 1
        ambiguous = possible_split or (len(candidates) > 1 and (best[0] - candidates[1][0]) < ambiguity_margin)
        status = "ambiguous" if ambiguous else "proposed"
        if ambiguous:
            result["ambiguous"] += 1
        else:
            result["matched"] += 1
        evidence = dict(best[2])
        evidence["alternatives"] = [{"feature_id": c[1].id, "score": c[0]} for c in candidates[1:3]]
        evidence["ambiguity_margin"] = ambiguity_margin
        evidence["possible_split_merge"] = possible_split
        proposal = MatchProposal(id=uid(), project_id=project_id, left_feature_id=lf.id, right_feature_id=best[1].id,
                                 score=best[0], score_type="uncalibrated_model_score" if active_model else "uncalibrated_rule_score",
                                 model_version=active_model.version if active_model else "rules-v2",
                                 status=status, candidate_rank=1, evidence=evidence)
        db.add(proposal)
        result["proposals"].append({"id": proposal.id, "left_feature_id": lf.id, "right_feature_id": best[1].id,
                                     "status": status, "score": best[0], "evidence": evidence})
        proposals_by_right.setdefault(best[1].id, []).append((proposal, result["proposals"][-1]))
    for competing in proposals_by_right.values():
        if len(competing) > 1:
            for proposal, receipt in competing:
                if proposal.status == "proposed":
                    result["matched"] -= 1
                    result["ambiguous"] += 1
                proposal.status = receipt["status"] = "ambiguous"
                proposal.evidence = {**proposal.evidence, "one_to_one_conflict": True,
                                     "competing_proposal_ids": [p.id for p, _ in competing if p.id != proposal.id]}
                receipt["evidence"] = proposal.evidence
    touch_project(db, project_id)
    finish_stage(db)
    return result


def run_topology(db: Session, project_id: str, dataset_id: str) -> dict[str, int]:
    dataset = db.get(Dataset, dataset_id)
    features = list(db.scalars(select(SourceFeature).where(SourceFeature.dataset_id == dataset_id).order_by(SourceFeature.id)))
    counts: dict[str, int] = {}
    if not dataset or not dataset.declared_crs:
        raise ValueError("Topology requires resolved source CRS")
    existing_conflicts = {(conflict.feature_id, conflict.conflict_type,
                           (conflict.details or {}).get("other_feature_id"))
                          for conflict in db.scalars(select(TopologyConflict).where(
                              TopologyConflict.project_id == project_id, TopologyConflict.dataset_id == dataset_id))}
    def record(feature_id, kind, severity, description, other=None, area=None):
        key = (feature_id, kind, other)
        counts[kind] = counts.get(kind, 0) + 1
        if key not in existing_conflicts:
            db.add(TopologyConflict(project_id=project_id, dataset_id=dataset_id, feature_id=feature_id,
                                   conflict_type=kind, severity=severity, description=description,
                                   details={"other_feature_id": other, "overlap_area_m2": area,
                                            "analysis_crs": dataset.analysis_crs}))
            existing_conflicts.add(key)

    usable = []
    for feature in features:
        original = geometry_from_feature({"geometry": feature.original_geometry})
        if original is not None and not original.is_valid:
            record(feature.id, "invalid_geometry", "error", explain_validity(original))
        geometry = geom_for(feature)
        if geometry is not None and geometry.is_valid and not geometry.is_empty:
            usable.append((feature, metric_geometry(geometry, dataset.analysis_crs)))
    tree = STRtree([geometry for _, geometry in usable])
    for i, (left, lg) in enumerate(usable):
        for index in tree.query(lg, predicate="intersects"):
            j = int(index)
            if j <= i:
                continue
            right, rg = usable[j]
            area = lg.intersection(rg).area
            if lg.equals(rg):
                record(left.id, "duplicate_geometry", "error", "Identical source geometries", right.id, area)
            elif area > 1e-6:
                record(left.id, "overlap", "warning", "Overlap or containment requires coverage review", right.id, area)
    policy = processing_policy(db, project_id)
    if policy.get('coverage_boundary') and dataset.analysis_crs:
        from shapely.ops import unary_union
        from shapely.geometry import Polygon
        coverage = metric_geometry(shape(policy['coverage_boundary']), dataset.analysis_crs)
        geometries = [geometry for _, geometry in usable]
        protected_holes = [Polygon(ring) for geometry in geometries
                           for polygon in (list(geometry.geoms) if geometry.geom_type == 'MultiPolygon' else [geometry])
                           if polygon.geom_type == 'Polygon' for ring in polygon.interiors]
        gaps = coverage.difference(unary_union([*geometries, *protected_holes]))
        if gaps.area > policy['overlap_tolerance_m2']:
            record(None, 'potential_coverage_gap', 'warning', 'Uncovered area within the explicit coverage boundary; source holes preserved', area=gaps.area)
    touch_project(db, project_id)
    finish_stage(db)
    return counts


def finish_stage(db: Session) -> None:
    """A worker owns its transaction; direct API calls commit at the stage boundary."""
    db.flush()
    if not db.info.get("job_transaction"):
        db.commit()


def run_change_detection(db: Session, project_id: str, before_dataset_id: str, after_dataset_id: str,
                         id_fields: list[str], tolerance: float) -> dict[str, int]:
    before_dataset = db.get(Dataset, before_dataset_id)
    after_dataset = db.get(Dataset, after_dataset_id)
    if not before_dataset or not after_dataset:
        raise ValueError("Both comparison datasets must exist")
    if not before_dataset.capture_date or not after_dataset.capture_date:
        raise ValueError("Both snapshot capture dates are required")
    if before_dataset.capture_date and after_dataset.capture_date and before_dataset.capture_date >= after_dataset.capture_date:
        raise ValueError("Baseline capture date must precede comparison capture date")
    before = list(db.scalars(select(SourceFeature).where(SourceFeature.dataset_id == before_dataset_id)))
    after = list(db.scalars(select(SourceFeature).where(SourceFeature.dataset_id == after_dataset_id)))
    membership = {link.source_feature_id: link.parcel_entity_id for link in db.scalars(select(ParcelSourceLink).where(
        ParcelSourceLink.project_id == project_id, ParcelSourceLink.link_status == "accepted"))}
    before_groups: dict[str, list[SourceFeature]] = {}
    after_groups: dict[str, list[SourceFeature]] = {}
    for feature in before:
        key = f"parcel:{membership[feature.id]}" if feature.id in membership else feature_key(feature, id_fields)
        if key:
            before_groups.setdefault(key, []).append(feature)
    for feature in after:
        key = f"parcel:{membership[feature.id]}" if feature.id in membership else feature_key(feature, id_fields)
        if key:
            after_groups.setdefault(key, []).append(feature)
    counts: dict[str, int] = {}
    for key in sorted(set(before_groups) | set(after_groups)):
        old_group, new_group = before_groups.get(key, []), after_groups.get(key, [])
        if len(old_group) > 1 or len(new_group) > 1:
            db.add(ChangeProposal(project_id=project_id, change_type="ambiguous_identity", boundary_change=False,
                                  status="needs_field_verification", evidence={"namespace_key": key,
                                                                                "before_feature_ids": [item.id for item in old_group],
                                                                                "after_feature_ids": [item.id for item in new_group],
                                                                                "reason": "Duplicate namespace identifiers cannot be compared automatically"}))
            counts["ambiguous_identity"] = counts.get("ambiguous_identity", 0) + 1
            continue
        old, new = (old_group[0] if old_group else None), (new_group[0] if new_group else None)
        if old and new and (old.id not in membership or membership.get(old.id) != membership.get(new.id)):
            db.add(ChangeProposal(project_id=project_id, change_type="identity_review_required", boundary_change=False,
                                  source_feature_id=new.id, comparison_feature_id=old.id, status="needs_field_verification",
                                  evidence={"namespace_key": key, "reason": "Snapshot correspondence requires accepted identity linkage"}))
            counts["identity_review_required"] = counts.get("identity_review_required", 0) + 1
            continue
        area_delta = None
        spatial_details: dict[str, Any] = {}
        affected_neighbors: list[str] = []
        if old and not new:
            kind, boundary = "missing", False
        elif new and not old:
            kind, boundary = "added", False
        else:
            old_geom, new_geom = geom_for(old), geom_for(new)
            metric_crs = after_dataset.analysis_crs or before_dataset.analysis_crs
            old_metric = metric_geometry(old_geom, metric_crs)
            new_metric = metric_geometry(new_geom, metric_crs)
            geometry_changed = bool(old_metric and new_metric and old_metric.hausdorff_distance(new_metric) > tolerance)
            attrs_changed = (old.canonical_attributes or old.raw_attributes) != (new.canonical_attributes or new.raw_attributes)
            if not geometry_changed and not attrs_changed:
                continue
            kind, boundary = ("geometry_changed" if geometry_changed else "attribute_changed"), geometry_changed
            if geometry_changed and old_geom and new_geom:
                old_area = old_metric.area
                new_area = new_metric.area
                area_delta = new_area - old_area
                centroid_shift = (old_metric.centroid.distance(new_metric.centroid)
                                  if old_metric and new_metric else None)
                spatial_details = {"before_area_m2": old_area, "after_area_m2": new_area,
                                   "centroid_shift_m": centroid_shift,
                                   "hausdorff_distance_m": (old_metric.hausdorff_distance(new_metric)
                                                            if old_metric and new_metric else None),
                                   "analysis_crs": metric_crs}
                affected_neighbors = [candidate.id for candidate in after
                                      if candidate.id != new.id and geom_for(candidate)
                                      and new_metric.distance(metric_geometry(geom_for(candidate), metric_crs)) <= tolerance]
        db.add(ChangeProposal(project_id=project_id, change_type=kind,
                              source_feature_id=new.id if new else old.id, comparison_feature_id=old.id if old and new else None,
                              before_geometry=old.normalized_geometry if old else None, after_geometry=new.normalized_geometry if new else None,
                               before_attributes=old.raw_attributes if old else None, after_attributes=new.raw_attributes if new else None,
                               area_delta=area_delta, affected_neighbors=affected_neighbors,
                               boundary_change=boundary, status="proposed",
                               evidence={"namespace_key": key, "tolerance": tolerance,
                                         "before_capture_date": before_dataset.capture_date.isoformat() if before_dataset.capture_date else None,
                                         "after_capture_date": after_dataset.capture_date.isoformat() if after_dataset.capture_date else None,
                                         "timestamps_are_snapshot_dates": True, **spatial_details}))
        counts[kind] = counts.get(kind, 0) + 1
    touch_project(db, project_id)
    finish_stage(db)
    return counts


def next_version(db: Session, project_id: str) -> int:
    return (db.scalar(select(func.max(PublishedVersion.version_number)).where(PublishedVersion.project_id == project_id)) or 0) + 1


def materialize_identity_link(db: Session, proposal: MatchProposal) -> ParcelEntity | None:
    """Turn one accepted identity decision into stable source-to-parcel links."""
    if proposal.status != "accepted" or not proposal.left_feature_id or not proposal.right_feature_id:
        return None
    if proposal.evidence.get("possible_split_merge"):
        raise ValueError("Possible split/merge requires field verification and explicit membership revisions")
    left_link = db.scalar(select(ParcelSourceLink).where(ParcelSourceLink.source_feature_id == proposal.left_feature_id))
    right_link = db.scalar(select(ParcelSourceLink).where(ParcelSourceLink.source_feature_id == proposal.right_feature_id))
    entity = db.get(ParcelEntity, left_link.parcel_entity_id if left_link else right_link.parcel_entity_id) if (left_link or right_link) else None
    if left_link and right_link and left_link.parcel_entity_id != right_link.parcel_entity_id:
        raise ValueError("Two existing canonical identities require an explicit merge revision")
    left_feature = db.get(SourceFeature, proposal.left_feature_id)
    if not entity:
        entity = ParcelEntity(project_id=proposal.project_id,
                              canonical_key=feature_key(left_feature, ID_ALIASES) if left_feature else None)
        db.add(entity)
        db.flush()
    members = list(db.scalars(select(SourceFeature).join(ParcelSourceLink, ParcelSourceLink.source_feature_id == SourceFeature.id)
                              .where(ParcelSourceLink.parcel_entity_id == entity.id)))
    for feature_id in (proposal.left_feature_id, proposal.right_feature_id):
        feature = db.get(SourceFeature, feature_id)
        if any(member.dataset_id == feature.dataset_id and member.id != feature.id for member in members):
            raise ValueError("Competing one-to-one identity or split/merge requires field verification")
    for feature_id in (proposal.left_feature_id, proposal.right_feature_id):
        existing = db.scalar(select(ParcelSourceLink).where(ParcelSourceLink.source_feature_id == feature_id))
        if existing:
            existing.parcel_entity_id = entity.id
            existing.match_proposal_id = proposal.id
            existing.link_status = "accepted"
        else:
            db.add(ParcelSourceLink(project_id=proposal.project_id, parcel_entity_id=entity.id,
                                    source_feature_id=feature_id, match_proposal_id=proposal.id,
                                     link_status="accepted"))
    db.flush()
    return entity


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
                          capture_date=date(2024 if shift == 0 else 2025, 1, 1),
                          declared_crs="EPSG:4326", metadata_json={"synthetic": True, "not_legal_evidence": True})
        db.add(dataset)
        db.flush()
        ingest_dataset(db, dataset, json.dumps(collection).encode(), f"{name}.geojson", "application/geo+json")
        datasets.append(dataset)
    audit(db, "synthetic_bootstrap", actor_id, project_id, details={"count": count, "dataset_ids": [d.id for d in datasets]})
    db.commit()
    return {"synthetic": True, "count": count, "dataset_ids": [d.id for d in datasets], "warning": "Synthetic data does not establish cadastral accuracy"}
