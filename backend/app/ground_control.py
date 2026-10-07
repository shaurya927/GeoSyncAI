"""Control fitting with metre residuals and verified source-coordinate coverage."""
import math

from pyproj import CRS
from shapely.geometry import MultiPoint, Point, mapping, shape
from sqlalchemy import select

from .advanced import fit_control_points
from .models import SourceFeature
from .spatial import check_coordinates


def _predict(method, parameters, coordinate):
    x, y = coordinate[:2]
    if method == "translation":
        dx, dy = parameters["translation"]
        return [x + dx, y + dy]
    if method == "similarity":
        a, b = parameters["a"], parameters["b"]
        return [a*x - b*y + parameters["tx"], b*x + a*y + parameters["ty"]]
    px, py = parameters["x"], parameters["y"]
    return [px[0]*x + px[1]*y + px[2], py[0]*x + py[1]*y + py[2]]


def residual_metres(crs, predicted, observed):
    check_coordinates(Point(*predicted), crs.is_geographic)
    check_coordinates(Point(*observed), crs.is_geographic)
    if crs.is_geographic:
        return abs(crs.get_geod().inv(*predicted, *observed)[2])
    if not crs.is_projected or len(crs.axis_info) < 2:
        raise ValueError("Ground-control target must have geographic or projected horizontal coordinates")
    fx, fy = [axis.unit_conversion_factor for axis in crs.axis_info[:2]]
    if not all(math.isfinite(v) and v > 0 for v in (fx, fy)):
        raise ValueError("Ground-control target units cannot be converted to metres")
    return math.hypot((predicted[0]-observed[0])*fx, (predicted[1]-observed[1])*fy)


def control_coverage(db, dataset_id, controls):
    # Independent checkpoints can extend observed coverage only after their
    # residual gate passes. A rank-deficient hull never authorizes extrapolation.
    hull = MultiPoint([point["source"][:2] for point in controls]).convex_hull
    features = list(db.scalars(select(SourceFeature).where(SourceFeature.dataset_id == dataset_id)))
    outside = []
    spatial_count = 0
    for feature in features:
        if feature.original_geometry:
            spatial_count += 1
            geometry = shape(feature.original_geometry)
            if not geometry.is_valid or hull.geom_type != "Polygon" or not hull.covers(geometry):
                outside.append(feature.id)
    return {"policy": "validated fitting-control and independent-checkpoint hull; no extrapolation",
            "control_hull": mapping(hull), "spatial_feature_count": spatial_count,
            "outside_feature_count": len(outside), "outside_feature_ids": outside[:20],
            "covers_source": bool(spatial_count and hull.geom_type == "Polygon" and not outside)}


def fit_session(db, dataset, payload):
    source_crs = CRS.from_user_input(payload.source_crs or dataset.declared_crs)
    target_crs = CRS.from_user_input(payload.target_crs or source_crs)
    if not dataset.declared_crs or not source_crs.equals(CRS.from_user_input(dataset.declared_crs), ignore_axis_order=True):
        raise ValueError("Control source CRS must match the immutable source coordinates' declared CRS")
    for control in payload.control_points:
        for field, crs in (("source", source_crs), ("target", target_crs)):
            coordinate = control.get(field)
            if not isinstance(coordinate, list) or len(coordinate) != 2 or not all(
                    isinstance(v, (int, float)) and math.isfinite(v) for v in coordinate):
                raise ValueError("Controls require finite two-dimensional source and target coordinates")
            check_coordinates(Point(*coordinate), crs.is_geographic)
    fitting = [point for point in payload.control_points if not point.get("checkpoint")]
    checkpoints = [point for point in payload.control_points if point.get("checkpoint")]
    fit = fit_control_points(payload.method, fitting, checkpoints)
    raw_fit = {key: fit[key] for key in ("fitting_residuals", "checkpoint_residuals")}
    for prefix, controls in (("fitting", fitting), ("checkpoint", checkpoints)):
        errors = [residual_metres(target_crs, _predict(payload.method, fit["parameters"], point["source"]),
                                 point["target"]) for point in controls]
        fit[prefix + "_residuals"] = errors
        fit[prefix + "_max"] = max(errors) if errors else None
        fit[prefix + "_rms"] = math.sqrt(sum(v*v for v in errors)/len(errors)) if errors else None
    fit.update(residual_units="metres", raw_target_residuals=raw_fit,
               target_coordinate_units=[axis.unit_name for axis in target_crs.axis_info[:2]],
               max_checkpoint_residual_threshold=payload.max_checkpoint_residual,
               source_content_hash=dataset.content_hash,
               coverage=control_coverage(db, dataset.id, payload.control_points))
    return source_crs.to_string(), target_crs.to_string(), fit
