"""Additive Phase A-D domain helpers.

These functions deliberately return evidence and gates alongside recommendations.
They do not adjudicate ownership, silently repair source bytes, or turn scores into
probabilities without a calibrated artifact.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date
from typing import Any

import numpy as np
from shapely.geometry import shape, mapping
from pyproj import CRS, Geod

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (Dataset, ModelArtifact, ParcelEntity, ParcelSelection, ParcelSourceLink,
                     ReconciliationCase, SourceFeature, TrainingExample)
from .services import audit, analysis_crs_for, metric_geometry, touch_project
from .spatial import check_coordinates, reproject


def dataset_metadata(dataset: Dataset) -> dict[str, Any]:
    return {
        "id": dataset.id, "name": dataset.name, "source_organization": dataset.source_organization,
        "capture_date": dataset.capture_date, "uploaded_at": dataset.uploaded_at,
        "administrative_namespace": dataset.administrative_namespace or {},
        "license_classification": dataset.license_classification,
        "access_classification": dataset.access_classification,
        "accuracy_metadata": dataset.accuracy_metadata or {}, "provenance": dataset.provenance or {},
        "source_version": dataset.source_version, "version_label": dataset.version_label,
        "content_hash": dataset.content_hash, "parent_dataset_id": dataset.parent_dataset_id,
        "declared_crs": dataset.declared_crs, "normalized_crs": dataset.normalized_crs,
        "analysis_crs": dataset.analysis_crs, "crs_transform": dataset.crs_transform,
        "status": dataset.status, "record_count": dataset.record_count,
        "normalized_count": dataset.normalized_count,
        "validation_report": dataset.validation_report,
    }


def _scoped_features(db: Session, project_id: str, feature_ids: list[str]) -> list[SourceFeature]:
    features = [db.get(SourceFeature, feature_id) for feature_id in feature_ids]
    if any(feature is None for feature in features):
        raise ValueError("Every source feature must exist")
    datasets = {dataset.id: dataset for dataset in db.scalars(select(Dataset).where(Dataset.project_id == project_id))}
    if any(feature.dataset_id not in datasets for feature in features):
        raise ValueError("Source feature is outside this project")
    if len({feature.dataset_id for feature in features}) != len(features):
        raise ValueError("Explicit multi-source evidence requires one feature per source dataset")
    return features


def build_reconciliation(db: Session, project_id: str, feature_ids: list[str], anchor_id: str | None,
                         actor_id: str, rationale: str | None) -> ReconciliationCase:
    features = _scoped_features(db, project_id, feature_ids)
    datasets = {dataset.id: dataset for dataset in db.scalars(select(Dataset).where(Dataset.project_id == project_id))}
    anchor = next((feature for feature in features if feature.id == anchor_id), features[0])
    fields = sorted({key for feature in features for key in (feature.canonical_attributes or feature.raw_attributes)})
    recommendation: dict[str, Any] = {}
    competing: dict[str, Any] = {}
    evidence: list[dict[str, Any]] = []
    for feature in features:
        dataset = datasets[feature.dataset_id]
        values = feature.canonical_attributes or feature.raw_attributes
        evidence.append({"feature_id": feature.id, "dataset_id": dataset.id, "organization": dataset.source_organization,
                         "capture_date": str(dataset.capture_date) if dataset.capture_date else None,
                         "content_hash": dataset.content_hash, "fields": values,
                         "geometry_available": bool(feature.normalized_geometry),
                         "independent_source": True})
    for field in fields:
        values = [{"feature_id": feature.id, "dataset_id": feature.dataset_id,
                   "value": (feature.canonical_attributes or feature.raw_attributes).get(field)} for feature in features
                  if field in (feature.canonical_attributes or feature.raw_attributes)]
        normalized = {json.dumps(item["value"], sort_keys=True, default=str) for item in values}
        if len(normalized) == 1:
            recommendation[field] = {"value": values[0]["value"], "support": len(values), "source_feature_ids": [v["feature_id"] for v in values]}
        else:
            competing[field] = values
    case = ReconciliationCase(project_id=project_id, anchor_feature_id=anchor.id,
                              source_feature_ids=[feature.id for feature in features],
                              recommendation=recommendation, competing_values=competing,
                              evidence=evidence, rationale=rationale, created_by=actor_id)
    db.add(case)
    touch_project(db, project_id)
    audit(db, "multi_source_reconciliation_created", actor_id, project_id, "reconciliation", case.id,
          {"source_feature_ids": feature_ids, "field_count": len(fields), "competing_fields": sorted(competing)})
    db.flush()
    return case


def measure_geometry(geometry_data: dict[str, Any], source_crs: str, analysis_crs: str | None,
                     purpose: str, method: str = "projected") -> dict[str, Any]:
    geometry = shape(geometry_data)
    try:
        source = CRS.from_user_input(source_crs)
    except Exception as exc:
        raise ValueError(f"Invalid source CRS: {source_crs}") from exc
    check_coordinates(geometry, geographic=source.is_geographic)
    if geometry.is_empty:
        raise ValueError("Empty geometry cannot be measured")
    bounds = geometry.bounds
    extent_gate: list[str] = []
    display_geometry = geometry if source.to_string() == "EPSG:4326" else reproject(geometry, source_crs, "EPSG:4326")
    display_bounds = display_geometry.bounds
    if source.is_geographic and display_bounds[2] - display_bounds[0] > 180:
        extent_gate.append("antimeridian_spanning_geometry_requires_explicit_split")
    if geometry.has_z:
        extent_gate.append("vertical_reference_not_supplied; Z is not used for area")
    if purpose == "display" or method == "display":
        return {"source_crs": source_crs, "analysis_crs": None, "axis_order": "always_xy", "purpose": purpose,
                "method": "display_only", "extent": {"min_x": display_bounds[0], "min_y": display_bounds[1],
                "max_x": display_bounds[2], "max_y": display_bounds[3]}, "area_m2": None, "length_m": None,
                "perimeter_m": None, "units": None, "gates": ["display_only_not_a_metric_or_cadastral_measurement"],
                "display_geometry": mapping(display_geometry), "publishable_measurement": False}
    if method == "geodesic":
        if display_bounds[2] - display_bounds[0] > 180:
            raise ValueError("Geodesic antimeridian-spanning measurement requires an explicit split")
        geod = Geod(ellps="WGS84")
        try:
            area, perimeter = geod.geometry_area_perimeter(display_geometry)
        except Exception as exc:
            raise ValueError("Geodesic measurement requires a supported polygon/line geometry") from exc
        return {"source_crs": source_crs, "analysis_crs": "WGS84 ellipsoid", "axis_order": "always_xy",
                "purpose": purpose, "method": "geodesic_wgs84", "extent": {"min_x": display_bounds[0], "min_y": display_bounds[1],
                "max_x": display_bounds[2], "max_y": display_bounds[3]}, "area_m2": abs(area) if geometry.geom_type in {"Polygon", "MultiPolygon"} else None,
                "length_m": perimeter, "perimeter_m": perimeter if geometry.geom_type in {"Polygon", "MultiPolygon"} else None,
                "units": "metres", "gates": extent_gate, "display_geometry": mapping(display_geometry),
                "publishable_measurement": not extent_gate}
    try:
        target = CRS.from_user_input(analysis_crs) if analysis_crs else (source if source.is_projected else CRS.from_user_input(analysis_crs_for(display_geometry)))
    except Exception as exc:
        raise ValueError("Invalid or unavailable projected analysis CRS") from exc
    if not target.is_projected:
        raise ValueError("A geographic analysis CRS cannot be used for metric area/length; choose projected or geodesic method")
    if source.is_geographic and display_bounds[2] - display_bounds[0] > 6 and not analysis_crs:
        raise ValueError("Automatic local UTM measurement is gated for multi-zone extents; supply an explicit analysis CRS")
    area_of_use = target.area_of_use
    if area_of_use and not (area_of_use.west <= display_bounds[0] and area_of_use.east >= display_bounds[2]
                            and area_of_use.south <= display_bounds[1] and area_of_use.north >= display_bounds[3]):
        raise ValueError("Geometry lies outside the selected analysis CRS area of use")
    chosen = target.to_string()
    metric = geometry if source == target else reproject(geometry, source_crs, chosen)
    unit_factor = target.axis_info[0].unit_conversion_factor if target.axis_info else 1.0
    if not unit_factor or not math.isfinite(unit_factor):
        raise ValueError("Analysis CRS has no finite linear-unit conversion")
    result = {"source_crs": source_crs, "analysis_crs": chosen, "axis_order": "always_xy", "purpose": purpose,
              "method": "projected", "extent": {"min_x": display_bounds[0], "min_y": display_bounds[1],
              "max_x": display_bounds[2], "max_y": display_bounds[3]},
              "area_m2": metric.area * unit_factor ** 2 if geometry.geom_type in {"Polygon", "MultiPolygon"} else None,
              "length_m": metric.length * unit_factor, "perimeter_m": metric.length * unit_factor if geometry.geom_type in {"Polygon", "MultiPolygon"} else None,
              "analysis_units": target.axis_info[0].unit_name if target.axis_info else "unknown",
              "unit_conversion_to_m": unit_factor, "units": "metres", "gates": extent_gate, "display_geometry": mapping(display_geometry),
              "publishable_measurement": not extent_gate}
    if extent_gate and purpose == "cadastral_review":
        result["publishable_measurement"] = False
    return result


def fit_control_points(method: str, fitting_points: list[dict[str, Any]], checkpoints: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Fit a 2D transform using only fitting points; checkpoints are never in the fit."""
    checkpoints = checkpoints or []
    if not fitting_points:
        raise ValueError("At least one non-checkpoint control is required")
    try:
        source = np.array([point["source"][:2] for point in fitting_points], dtype=float)
        target = np.array([point["target"][:2] for point in fitting_points], dtype=float)
        check_source = np.array([point["source"][:2] for point in checkpoints], dtype=float)
        check_target = np.array([point["target"][:2] for point in checkpoints], dtype=float)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Control points need finite source and target coordinate pairs") from exc
    if not np.isfinite(source).all() or not np.isfinite(target).all() or (len(checkpoints) and (not np.isfinite(check_source).all() or not np.isfinite(check_target).all())):
        raise ValueError("Control points must contain finite coordinates")
    if method == "translation":
        if len(fitting_points) < 1:
            raise ValueError("Translation fitting requires one control")
        translation = np.mean(target - source, axis=0)
        predicted = source + translation
        check_predicted = check_source + translation
        parameters: dict[str, Any] = {"translation": translation.tolist()}
    elif method == "similarity":
        if len(fitting_points) < 2 or np.linalg.matrix_rank(source[1:] - source[0]) < 1:
            raise ValueError("Similarity fitting requires two distinct controls")
        design = np.zeros((len(fitting_points) * 2, 4))
        design[0::2, :] = np.column_stack((source[:, 0], -source[:, 1], np.ones(len(source)), np.zeros(len(source))))
        design[1::2, :] = np.column_stack((source[:, 1], source[:, 0], np.zeros(len(source)), np.ones(len(source))))
        targets = target.reshape(-1)
        if np.linalg.matrix_rank(design) < 4:
            raise ValueError("Similarity controls are geometrically degenerate")
        coefficients = np.linalg.lstsq(design, targets, rcond=None)[0]
        a, b, tx, ty = coefficients
        predicted = np.column_stack((a * source[:, 0] - b * source[:, 1] + tx,
                                      b * source[:, 0] + a * source[:, 1] + ty))
        check_predicted = np.column_stack((a * check_source[:, 0] - b * check_source[:, 1] + tx,
                                            b * check_source[:, 0] + a * check_source[:, 1] + ty)) if len(checkpoints) else np.empty((0, 2))
        parameters = {"a": float(a), "b": float(b), "tx": float(tx), "ty": float(ty),
                      "scale": float(math.hypot(a, b)), "rotation_rad": float(math.atan2(b, a))}
    elif method == "affine":
        if len(fitting_points) < 3:
            raise ValueError("Affine fitting requires at least three controls")
        design = np.column_stack((source, np.ones(len(source))))
        if np.linalg.matrix_rank(design) < 3:
            raise ValueError("Affine controls are collinear or geometrically degenerate")
        x = np.linalg.lstsq(design, target[:, 0], rcond=None)[0]
        y = np.linalg.lstsq(design, target[:, 1], rcond=None)[0]
        predicted = np.column_stack((design @ x, design @ y))
        check_design = np.column_stack((check_source, np.ones(len(check_source)))) if len(checkpoints) else np.empty((0, 3))
        check_predicted = np.column_stack((check_design @ x, check_design @ y)) if len(checkpoints) else np.empty((0, 2))
        parameters = {"x": x.tolist(), "y": y.tolist()}
    else:
        raise ValueError(f"Unsupported control-point method: {method}")
    fit_residuals = np.linalg.norm(predicted - target, axis=1)
    checkpoint_residuals = np.linalg.norm(check_predicted - check_target, axis=1) if len(checkpoints) else np.array([])
    return {"method": method, "parameters": parameters,
            "fitting_residuals": [float(value) for value in fit_residuals],
            "fitting_rms": float(math.sqrt(np.mean(fit_residuals ** 2))),
            "fitting_max": float(np.max(fit_residuals)), "fitting_count": len(fitting_points),
            "checkpoint_residuals": [float(value) for value in checkpoint_residuals],
            "checkpoint_rms": float(math.sqrt(np.mean(np.square(checkpoint_residuals)))) if len(checkpoints) else None,
            "checkpoint_max": float(np.max(checkpoint_residuals)) if len(checkpoints) else None,
            "checkpoint_count": len(checkpoints),
            "extrapolation_gate": "control-point hull only; outside-hull application requires independent evidence"}


def geometry_measurements(db: Session, parcel_ids: list[str], draft_geometries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    before: dict[str, float] = {}
    before_geometries: dict[str, dict[str, Any]] = {}
    after: dict[str, float] = {}
    max_displacement = 0.0
    entities = {entity.id: entity for entity in db.scalars(select(ParcelEntity).where(ParcelEntity.id.in_(parcel_ids)))}
    links = list(db.scalars(select(ParcelSourceLink).where(ParcelSourceLink.parcel_entity_id.in_(parcel_ids))))
    sources = {feature.id: feature for feature in db.scalars(select(SourceFeature).where(SourceFeature.id.in_([link.source_feature_id for link in links])))}
    selections = {selection.parcel_entity_id: selection for selection in db.scalars(select(ParcelSelection).where(ParcelSelection.parcel_entity_id.in_(parcel_ids)))}
    for parcel_id in parcel_ids:
        geometry = None
        selection = selections.get(parcel_id)
        if selection and selection.geometry_source_id:
            selected_source = db.get(SourceFeature, selection.geometry_source_id)
            if selected_source and selected_source.normalized_geometry:
                geometry = shape(selected_source.normalized_geometry)
        if geometry is not None:
            chosen = analysis_crs_for(geometry)
            before[parcel_id] = metric_geometry(geometry, chosen).area
            before_geometries[parcel_id] = mapping(geometry)
    for draft_id, draft_data in draft_geometries.items():
        draft = shape(draft_data)
        check_coordinates(draft, True)
        if not draft.is_valid or draft.geom_type not in {"Polygon", "MultiPolygon"}:
            raise ValueError(f"Draft geometry for {draft_id} must be a valid polygon")
        chosen = analysis_crs_for(draft)
        after[draft_id] = metric_geometry(draft, chosen).area
        if draft_id in before_geometries:
            baseline = shape(before_geometries[draft_id])
            max_displacement = max(max_displacement, metric_geometry(baseline, chosen).hausdorff_distance(metric_geometry(draft, chosen)))
    return {"before_area_m2": before, "after_area_m2": after, "before_geometries": before_geometries,
            "area_delta_m2": sum(after.values()) - sum(before.values()),
            "area_conservation_delta_m2": sum(after.values()) - sum(before.values()),
            "max_displacement_m": max_displacement,
            "analysis_crs": sorted({analysis_crs_for(shape(geometry)) for geometry in draft_geometries.values()})}


def feature_vector(evidence: dict[str, Any]) -> dict[str, float]:
    return {"score": float(evidence.get("score", 0.0)), "identifier_agreement": float(bool(evidence.get("identifier_agreement"))),
            "iou": float(evidence.get("intersection_over_union", 0.0) or 0.0),
            "distance_inverse": 1.0 / (1.0 + float(evidence.get("centroid_distance_m") or 100000.0)),
            "namespace_compatible": float(bool(evidence.get("namespace_compatible", True)))}


def train_ranker(db: Session, project_id: str, actor_id: str, seed: int, version: str) -> ModelArtifact:
    examples = list(db.scalars(select(TrainingExample).where(TrainingExample.project_id == project_id).order_by(TrainingExample.id)))
    if not examples:
        raise ValueError("At least one reviewed training example is required")
    groups: dict[str, str] = {}
    for example in examples:
        prior = groups.get(example.group_key)
        if prior and prior != example.split:
            raise ValueError("Related parcel/source groups cannot be split across train/calibration/validation/test")
        groups[example.group_key] = example.split
    train = [example for example in examples if example.split == "train"]
    if not train or {example.label for example in train} != {0, 1}:
        raise ValueError("Training split must contain both positive and negative reviewed labels")
    fields = sorted({field for example in examples for field in example.features})
    x = np.array([[float(example.features.get(field, 0.0)) for field in fields] for example in train], dtype=float)
    y = np.array([example.label for example in train], dtype=float)
    weights = np.zeros(len(fields), dtype=float)
    bias = 0.0
    rng = np.random.default_rng(seed)
    weights += rng.normal(0, 1e-9, len(fields))
    for _ in range(500):
        logits = np.clip(x @ weights + bias, -30, 30)
        probabilities = 1 / (1 + np.exp(-logits))
        gradient = x.T @ (probabilities - y) / len(y) + 0.01 * weights
        weights -= 0.25 * gradient
        bias -= 0.25 * float(np.mean(probabilities - y))
    fingerprint = hashlib.sha256(json.dumps([example.features | {"label": example.label, "group": example.group_key} for example in examples], sort_keys=True).encode()).hexdigest()

    def metrics(rows: list[TrainingExample]) -> dict[str, Any]:
        if not rows:
            return {"count": 0}
        matrix = np.array([[float(row.features.get(field, 0.0)) for field in fields] for row in rows], dtype=float)
        probs = 1 / (1 + np.exp(-np.clip(matrix @ weights + bias, -30, 30)))
        labels = np.array([row.label for row in rows])
        predictions = probs >= 0.5
        tp = int(np.sum(predictions & (labels == 1))); fp = int(np.sum(predictions & (labels == 0)))
        fn = int(np.sum(~predictions & (labels == 1)))
        return {"count": len(rows), "precision": tp / max(tp + fp, 1), "recall": tp / max(tp + fn, 1),
                "brier": float(np.mean((probs - labels) ** 2)),
                "reliability": [{"bin": i, "count": int(np.sum((probs >= i / 5) & (probs < (i + 1) / 5))),
                                 "mean_score": float(np.mean(probs[(probs >= i / 5) & (probs < (i + 1) / 5)])) if np.any((probs >= i / 5) & (probs < (i + 1) / 5)) else None,
                                 "positive_rate": float(np.mean(labels[(probs >= i / 5) & (probs < (i + 1) / 5)])) if np.any((probs >= i / 5) & (probs < (i + 1) / 5)) else None} for i in range(5)]}
    metrics_by_split = {split: metrics([example for example in examples if example.split == split]) for split in {example.split for example in examples}}
    artifact = ModelArtifact(project_id=project_id, model_type="supervised_logistic_ranker", version=version,
                             artifact={"fields": fields, "weights": weights.tolist(), "bias": bias,
                                       "score_type": "uncalibrated_model_score", "probability_claim": False,
                                       "default_matcher": "rules-v2"},
                             metrics={"splits": metrics_by_split, "baseline": "deterministic rules-v2", "routing": "validation-derived; review abstention remains enabled"},
                             dataset_fingerprint=fingerprint, seed=seed, created_by=actor_id)
    db.add(artifact)
    audit(db, "ranker_trained", actor_id, project_id, "model_artifact", artifact.id,
          {"version": version, "fingerprint": fingerprint, "metrics": metrics_by_split})
    db.flush()
    return artifact


def parse_structured_query(query: str) -> dict[str, Any]:
    normalized = " ".join(query.casefold().split())
    result: dict[str, Any] = {"kind": "help", "filters": {}, "limit": 100}
    if re.search(r"conflict|विवाद", normalized):
        result["kind"] = "conflicts"
        ward = re.search(r"(?:ward|वार्ड)\s*[:=]?\s*([\w-]+)", normalized)
        if ward:
            result["filters"]["ward"] = ward.group(1)
    elif re.search(r"missing\s+(?:link|links)|लिंक.*नहीं|unmatched", normalized):
        result["kind"] = "missing_links"
    elif re.search(r"area|क्षेत्रफल", normalized):
        result["kind"] = "area_threshold"
        match = re.search(r"(?:area|क्षेत्रफल)[^0-9]{0,30}(\d+(?:\.\d+)?)", normalized)
        if match:
            result["filters"]["area_threshold"] = float(match.group(1))
        else:
            result["clarification"] = "Specify an area threshold in the source units."
    elif re.search(r"near|nearby|पास|निकट", normalized):
        result["kind"] = "nearby_tasks"
    elif re.search(r"change|परिवर्तन|dated|तिथि", normalized):
        result["kind"] = "dated_changes"
    return result


SUPPORTED_COMPLIANCE_OPERATORS = {"<", "<=", ">", ">=", "==", "between", "ratio_le", "ratio_ge"}


def validate_compliance_rule(rule_data: dict[str, Any]) -> None:
    formula = rule_data.get("formula") or {}
    threshold = rule_data.get("threshold") or {}
    operator = rule_data.get("operator") or formula.get("operator") or threshold.get("operator") or "<="
    if operator not in SUPPORTED_COMPLIANCE_OPERATORS:
        raise ValueError(f"Unsupported compliance operator: {operator}")
    if operator == "between" and (threshold.get("min") is None or threshold.get("max") is None):
        raise ValueError("A between rule requires min and max thresholds")
    if operator != "between" and threshold.get("value", threshold.get("max", threshold.get("min"))) is None:
        raise ValueError("A compliance rule requires an explicit threshold")
    for value in (threshold.get("value"), threshold.get("min"), threshold.get("max")):
        if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(float(value))):
            raise ValueError("Compliance thresholds must be finite numbers")


def _convert_units(value: float, source_units: str | None, target_units: str | None) -> float:
    if not source_units or not target_units or source_units == target_units:
        return value
    factors = {"m": 1.0, "metre": 1.0, "metres": 1.0, "ft": 0.3048, "foot": 0.3048, "feet": 0.3048,
               "m2": 1.0, "m²": 1.0, "sq_m": 1.0, "ft2": 0.09290304, "sqft": 0.09290304, "sq_ft": 0.09290304}
    source_factor, target_factor = factors.get(source_units.casefold()), factors.get(target_units.casefold())
    if source_factor is None or target_factor is None:
        raise ValueError(f"Unsupported or incompatible units: {source_units} -> {target_units}")
    return value * source_factor / target_factor


def compliance_result(rule: Any, values: dict[str, Any]) -> dict[str, Any]:
    today = date.today()
    if (rule.effective_from and today < rule.effective_from) or (rule.effective_to and today > rule.effective_to):
        return {"status": "not_applicable", "reason": "Rule is outside its effective date range", "rule_id": rule.id, "rule_version": rule.version}
    required = [str(item.get("name")) for item in (rule.inputs or []) if item.get("required", True)]
    missing = [name for name in required if values.get(name) in (None, "")]
    if missing:
        return {"status": "insufficient_information", "missing_inputs": missing, "rule_id": rule.id, "rule_version": rule.version}
    formula = rule.formula or {}
    operation = formula.get("operation", rule.category)
    threshold = rule.threshold or {}
    operator = formula.get("operator") or threshold.get("operator") or "<="
    if operator not in SUPPORTED_COMPLIANCE_OPERATORS:
        raise ValueError(f"Unsupported compliance operator: {operator}")
    applies_if = formula.get("applies_if") or {}
    if any(values.get(key) != expected for key, expected in applies_if.items()):
        return {"status": "not_applicable", "reason": "Applicability conditions are not met", "rule_id": rule.id, "rule_version": rule.version}
    if operation in {"far", "fsi"} and values.get("floor_area_m2") is None:
        return {"status": "insufficient_information", "missing_inputs": ["floor_area_m2"], "explanation": "FAR/FSI cannot be inferred from footprint"}
    actual_name = formula.get("actual", "value")
    actual = values.get(actual_name)
    if operation in {"far", "fsi"} and actual is None:
        if values.get("plot_area_m2") in (None, 0):
            return {"status": "insufficient_information", "missing_inputs": ["plot_area_m2"], "rule_id": rule.id}
        actual = float(values["floor_area_m2"]) / float(values["plot_area_m2"])
        actual_name = "floor_area_m2 / plot_area_m2"
    threshold_units = threshold.get("units")
    input_units = next((item.get("units") for item in (rule.inputs or []) if item.get("name") == actual_name), threshold_units)
    if actual is None:
        return {"status": "insufficient_information", "missing_inputs": [actual_name], "rule_id": rule.id}
    try:
        actual = _convert_units(float(actual), input_units, threshold_units)
        if operator == "between":
            passed = _convert_units(float(threshold["min"]), threshold_units, threshold_units) <= actual <= _convert_units(float(threshold["max"]), threshold_units, threshold_units)
            limit: Any = {"min": threshold["min"], "max": threshold["max"]}
        elif operator in {"ratio_le", "ratio_ge"}:
            limit = float(threshold.get("value", threshold.get("max")))
            passed = actual <= limit if operator == "ratio_le" else actual >= limit
        else:
            limit = float(threshold.get("value", threshold.get("max", threshold.get("min"))))
            passed = {"<": actual < limit, "<=": actual <= limit, ">": actual > limit,
                      ">=": actual >= limit, "==": math.isclose(actual, limit, rel_tol=1e-9, abs_tol=1e-9)}[operator]
    except (TypeError, ValueError, KeyError):
        return {"status": "insufficient_information", "explanation": "Inputs must be numeric", "rule_id": rule.id}
    return {"status": "pass" if passed else "potential_violation", "actual": actual, "threshold": limit,
            "operator": operator, "units": threshold.get("units"), "rule_id": rule.id, "rule_version": rule.version,
            "explanation": f"{actual_name}={actual} compared with configured operator {operator} and threshold {limit}"}
