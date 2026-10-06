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
from typing import Any

import numpy as np
from shapely.geometry import shape, mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (Dataset, ModelArtifact, ParcelEntity, ParcelSourceLink,
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


def measure_geometry(geometry_data: dict[str, Any], source_crs: str, analysis_crs: str | None, purpose: str) -> dict[str, Any]:
    geometry = shape(geometry_data)
    check_coordinates(geometry, geographic=__import__("pyproj").CRS(source_crs).is_geographic)
    if geometry.is_empty:
        raise ValueError("Empty geometry cannot be measured")
    bounds = geometry.bounds
    extent_gate: list[str] = []
    if __import__("pyproj").CRS(source_crs).is_geographic and bounds[2] - bounds[0] > 180:
        extent_gate.append("antimeridian_spanning_geometry_requires_explicit_split")
    if geometry.has_z:
        extent_gate.append("vertical_reference_not_supplied; Z is not used for area")
    if purpose == "cadastral_review" and not analysis_crs:
        extent_gate.append("explicit_projected_analysis_crs_required_for_cadastral_review")
    chosen = analysis_crs or (analysis_crs_for(reproject(geometry, source_crs, "EPSG:4326")) if source_crs != "EPSG:4326" else analysis_crs_for(geometry))
    if not chosen:
        raise ValueError("A projected analysis CRS is required")
    metric = reproject(geometry, source_crs, chosen)
    result = {"source_crs": source_crs, "analysis_crs": chosen, "axis_order": "always_xy",
              "purpose": purpose, "extent": {"min_x": bounds[0], "min_y": bounds[1], "max_x": bounds[2], "max_y": bounds[3]},
              "area_m2": metric.area if geometry.geom_type in {"Polygon", "MultiPolygon"} else None,
              "length_m": metric.length, "perimeter_m": metric.length if geometry.geom_type in {"Polygon", "MultiPolygon"} else None,
              "gates": extent_gate, "display_geometry": mapping(reproject(geometry, source_crs, "EPSG:4326")) if source_crs != "EPSG:4326" else geometry_data}
    if extent_gate and purpose == "cadastral_review":
        result["publishable_measurement"] = False
    else:
        result["publishable_measurement"] = True
    return result


def fit_control_points(method: str, control_points: list[dict[str, Any]]) -> dict[str, Any]:
    """Fit a documented 2D transform without changing retained source bytes."""
    source = np.array([point["source"][:2] for point in control_points], dtype=float)
    target = np.array([point["target"][:2] for point in control_points], dtype=float)
    if method == "translation":
        translation = np.mean(target - source, axis=0)
        predicted = source + translation
        parameters: dict[str, Any] = {"translation": translation.tolist()}
    elif method == "similarity":
        if len(control_points) < 2:
            raise ValueError("Similarity fitting requires at least two points")
        matrix = np.column_stack((source[:, 0], -source[:, 1], np.ones(len(source))))
        x = np.linalg.lstsq(matrix, target[:, 0], rcond=None)[0]
        y = np.linalg.lstsq(np.column_stack((source[:, 1], source[:, 0], np.ones(len(source)))), target[:, 1], rcond=None)[0]
        predicted = np.column_stack((matrix @ x, np.column_stack((source[:, 1], source[:, 0], np.ones(len(source)))) @ y))
        parameters = {"a": float(x[0]), "b": float(y[1]), "tx": float(x[2]), "ty": float(y[2]),
                      "scale": float(math.hypot(x[0], y[1])), "rotation_rad": float(math.atan2(y[1], x[0]))}
    else:
        if len(control_points) < 3:
            raise ValueError("Affine fitting requires at least three points")
        design = np.column_stack((source, np.ones(len(source))))
        x = np.linalg.lstsq(design, target[:, 0], rcond=None)[0]
        y = np.linalg.lstsq(design, target[:, 1], rcond=None)[0]
        predicted = np.column_stack((design @ x, design @ y))
        parameters = {"x": x.tolist(), "y": y.tolist()}
    residuals = np.linalg.norm(predicted - target, axis=1)
    checkpoints = [float(residuals[index]) for index, point in enumerate(control_points) if point.get("checkpoint")]
    return {"method": method, "parameters": parameters, "residuals": [float(value) for value in residuals],
            "rms": float(math.sqrt(np.mean(residuals ** 2))), "max": float(np.max(residuals)),
            "independent_checkpoints": len(checkpoints), "checkpoint_rms": float(math.sqrt(np.mean(np.square(checkpoints)))) if checkpoints else None,
            "checkpoint_max": max(checkpoints) if checkpoints else None,
            "extrapolation_gate": "control-point hull only; outside-hull application requires independent evidence"}


def geometry_measurements(db: Session, parcel_ids: list[str], draft_geometries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    before: dict[str, float] = {}
    before_geometries: dict[str, dict[str, Any]] = {}
    after: dict[str, float] = {}
    max_displacement = 0.0
    entities = {entity.id: entity for entity in db.scalars(select(ParcelEntity).where(ParcelEntity.id.in_(parcel_ids)))}
    links = list(db.scalars(select(ParcelSourceLink).where(ParcelSourceLink.parcel_entity_id.in_(parcel_ids))))
    sources = {feature.id: feature for feature in db.scalars(select(SourceFeature).where(SourceFeature.id.in_([link.source_feature_id for link in links])))}
    for parcel_id in parcel_ids:
        entity_links = [link for link in links if link.parcel_entity_id == parcel_id]
        geometry = None
        for link in entity_links:
            if sources[link.source_feature_id].normalized_geometry:
                geometry = shape(sources[link.source_feature_id].normalized_geometry)
                break
        if geometry is not None:
            chosen = analysis_crs_for(geometry)
            before[parcel_id] = metric_geometry(geometry, chosen).area
            before_geometries[parcel_id] = mapping(geometry)
        if parcel_id in draft_geometries:
            draft = shape(draft_geometries[parcel_id])
            check_coordinates(draft, True)
            if not draft.is_valid or draft.geom_type not in {"Polygon", "MultiPolygon"}:
                raise ValueError(f"Draft geometry for {parcel_id} must be a valid polygon")
            chosen = analysis_crs_for(draft)
            after[parcel_id] = metric_geometry(draft, chosen).area
            if geometry is not None:
                max_displacement = max(max_displacement, metric_geometry(geometry, chosen).hausdorff_distance(metric_geometry(draft, chosen)))
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


def compliance_result(rule: Any, values: dict[str, Any]) -> dict[str, Any]:
    required = [str(item.get("name")) for item in (rule.inputs or []) if item.get("required", True)]
    missing = [name for name in required if values.get(name) in (None, "")]
    if missing:
        return {"status": "insufficient_information", "missing_inputs": missing, "rule_id": rule.id, "rule_version": rule.version}
    formula = rule.formula or {}
    operation = formula.get("operation", rule.category)
    threshold = rule.threshold or {}
    if operation in {"far", "fsi"} and values.get("floor_area_m2") is None:
        return {"status": "insufficient_information", "missing_inputs": ["floor_area_m2"], "explanation": "FAR/FSI cannot be inferred from footprint"}
    actual_name = formula.get("actual", "value")
    actual = values.get(actual_name)
    limit = threshold.get("max", threshold.get("value"))
    if actual is None or limit is None:
        return {"status": "insufficient_information", "missing_inputs": [actual_name, "threshold"], "rule_id": rule.id}
    try:
        passed = float(actual) <= float(limit)
    except (TypeError, ValueError):
        return {"status": "insufficient_information", "explanation": "Inputs must be numeric", "rule_id": rule.id}
    return {"status": "pass" if passed else "potential_violation", "actual": actual, "threshold": limit,
            "units": threshold.get("units"), "rule_id": rule.id, "rule_version": rule.version,
            "explanation": f"{actual_name}={actual} compared with configured maximum {limit}"}
