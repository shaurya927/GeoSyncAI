"""Independent verification for exported GeoSyncAI publication artifacts.

Example::
  PYTHONPATH=backend python -m app.verify_artifact --manifest lineage.json \
    --expected-manifest-sha256 <digest> --geojson published.geojson \
    --expected-output-sha256 <digest>
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify GeoSyncAI manifest and frozen output digests")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--geojson", type=Path)
    parser.add_argument("--expected-output-sha256")
    parser.add_argument("--source", action="append", default=[], metavar="PATH=SHA256")
    args = parser.parse_args()
    document = json.loads(args.manifest.read_text(encoding="utf-8"))
    manifest = document.get("manifest", document)
    actual_manifest = digest(manifest)
    results = {"manifest_sha256": actual_manifest, "manifest_valid": actual_manifest == args.expected_manifest_sha256}
    if args.geojson:
        collection = json.loads(args.geojson.read_text(encoding="utf-8"))
        output = sorted([{"parcel_entity_id": feature.get("id"), "geometry": feature.get("geometry"),
                          "attributes": {key: value for key, value in (feature.get("properties") or {}).items() if key != "_lineage"},
                          "lineage": (feature.get("properties") or {}).get("_lineage", {})}
                         for feature in collection.get("features", [])], key=lambda item: item["parcel_entity_id"] or "")
        results["output_sha256"] = digest(output)
        results["output_valid"] = bool(args.expected_output_sha256 and results["output_sha256"] == args.expected_output_sha256)
    source_results = []
    for specification in args.source:
        if "=" not in specification:
            parser.error("--source must be PATH=SHA256")
        path_text, expected = specification.rsplit("=", 1)
        path = Path(path_text)
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        source_results.append({"path": str(path), "expected_sha256": expected, "actual_sha256": actual, "valid": actual == expected})
    results["sources"] = source_results
    results["valid"] = bool(results["manifest_valid"] and results.get("output_valid", True)
                             and bool(source_results) and all(item["valid"] for item in source_results) if source_results else results["manifest_valid"] and results.get("output_valid", True))
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0 if results["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
