"""CRS and metric operations. Never interpret missing CRS as longitude/latitude."""
from functools import lru_cache, partial
import math

import pyproj
from pyproj import CRS, Transformer
from pyproj.transformer import TransformerGroup
from shapely import get_coordinates
from shapely.ops import transform


def check_coordinates(geometry, geographic=False):
    if geometry.is_empty:
        raise ValueError("Empty geometry")
    if geometry.geom_type not in {"Polygon", "MultiPolygon", "Point", "MultiPoint", "LineString", "MultiLineString"}:
        raise ValueError(f"Unsupported geometry type: {geometry.geom_type}")
    for point in get_coordinates(geometry, include_z=geometry.has_z):
        x, y = point[:2]
        if not all(math.isfinite(value) for value in point):
            raise ValueError("Nonfinite coordinate")
        if geographic and (abs(x) > 180 or abs(y) > 90):
            raise ValueError("Implausible longitude/latitude for geographic CRS")


@lru_cache(maxsize=128)
def transformer(source, target):
    return Transformer.from_crs(source, target, always_xy=True, allow_ballpark=False, only_best=True)


def reproject(geometry, source, target):
    check_coordinates(geometry, CRS(source).is_geographic)
    result = transform(partial(transformer(source, target).transform, errcheck=True), geometry)
    check_coordinates(result, CRS(target).is_geographic)
    return result


@lru_cache(maxsize=128)
def transformation_metadata(source, target="EPSG:4326"):
    group = TransformerGroup(source, target, always_xy=True, allow_ballpark=False)
    if not group.transformers or not group.best_available:
        raise ValueError("Best CRS transformation is unavailable; required grids must be installed")
    operation = group.transformers[0]
    return {"source_crs": source, "target_crs": target, "always_xy": True,
            "operation": operation.description, "definition": operation.definition,
            "operation_accuracy_m": operation.accuracy if operation.accuracy >= 0 else None,
            "grids": [{"name": g.short_name, "available": g.available}
                      for part in operation.operations for g in part.grids],
            "pyproj_version": pyproj.__version__, "proj_version": pyproj.proj_version_str,
            "boundary_accuracy_established": False}
