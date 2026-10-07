"""Approved canonical snapshots. Identity membership never chooses a boundary.

Policy v2: only reviewer-selected baselines are in scope. Unselected records are
listed as exclusions. Pending changes touching an in-scope entity block; rejected
changes retain the selected baseline. Deletions/splits require manual selection.
"""
import hashlib
import json

from sqlalchemy import select
from shapely.geometry import shape
from shapely.strtree import STRtree

from .models import (ChangeProposal, Dataset, MatchProposal, ParcelEntity, ParcelSelection,
                      ParcelSourceLink, PublicationFeature, PublishedVersion, ReviewDecision,
                      SchemaMapping, SourceFeature, GeometryChangeSet)
from .services import audit, metric_geometry, next_version, spatial_column
from .spatial import check_coordinates
from .policy import processing_policy

POLICY = {"version": "reviewed-baseline-v2", "unselected": "exclude_with_reason",
          "pending_changes": "block_selected_entities", "rejected_changes": "retain_selected_baseline",
          "overlap_tolerance_m2": 0.01}


def canonical_sha256(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def canonical_output_context(records) -> list[dict]:
    return sorted([{"parcel_entity_id": record["parcel_entity_id"], "geometry": record["geometry"],
                    "attributes": record["attributes"], "lineage": record["lineage"]} for record in records],
                   key=lambda item: item["parcel_entity_id"] or "")


def candidate_snapshot(db, project):
    policy = {**POLICY, 'processing': processing_policy(db, project.id)}
    datasets = {d.id: d for d in db.scalars(select(Dataset).where(Dataset.project_id == project.id))}
    sources = {f.id: f for f in db.scalars(select(SourceFeature).where(SourceFeature.dataset_id.in_(datasets)))}
    links = list(db.scalars(select(ParcelSourceLink).where(ParcelSourceLink.project_id == project.id)))
    matches = list(db.scalars(select(MatchProposal).where(MatchProposal.project_id == project.id, MatchProposal.status == "accepted")))
    changes = list(db.scalars(select(ChangeProposal).where(ChangeProposal.project_id == project.id, ChangeProposal.status != "superseded").order_by(ChangeProposal.created_at, ChangeProposal.id)))
    geometry_changes = list(db.scalars(select(GeometryChangeSet).where(GeometryChangeSet.project_id == project.id)
                                      .order_by(GeometryChangeSet.decision_at, GeometryChangeSet.created_at, GeometryChangeSet.id)))
    reviews = list(db.scalars(select(ReviewDecision).where(ReviewDecision.project_id == project.id).order_by(ReviewDecision.id)))
    mappings = {m.id: m for m in db.scalars(select(SchemaMapping).where(SchemaMapping.project_id == project.id))}
    selections = list(db.scalars(select(ParcelSelection).join(ParcelEntity, ParcelEntity.id == ParcelSelection.parcel_entity_id)
                                 .where(ParcelEntity.project_id == project.id, ParcelEntity.status == "active")
                                 .order_by(ParcelSelection.parcel_entity_id)))
    failures, excluded, output = [], [], []
    # Unresolved source references are a project-wide spatial publication gate.
    for d in datasets.values():
        report = d.validation_report or {}
        if (report.get("has_geometry") or d.geometry_type or d.status == "needs_crs_review") and d.normalized_crs != "EPSG:4326":
            failures.append({"dataset_id": d.id, "reason": f"CRS unresolved or implausible: {d.name}"})
    selected_ids = set()
    for selection in selections:
        members = sorted({link.source_feature_id for link in links if link.parcel_entity_id == selection.parcel_entity_id})
        selected_ids.update(members)
        attributes = sources.get(selection.attribute_source_id)
        geometry_source = sources.get(selection.geometry_source_id)
        if not attributes or attributes.status != "processed" or (selection.geometry_source_id and (not geometry_source or geometry_source.status != "processed")):
            failures.append({"parcel_entity_id": selection.parcel_entity_id, "reason": "Selected source is unavailable or quarantined"})
            continue
        geometry = geometry_source.normalized_geometry if geometry_source else None
        values = {**attributes.raw_attributes, **(attributes.canonical_attributes or {})}
        for field, source_id in (selection.attribute_sources or {}).items():
            source = sources.get(source_id)
            if not source or source.id not in members:
                failures.append({'parcel_entity_id':selection.parcel_entity_id, 'reason':'Attribute precedence source is no longer linked'})
                continue
            canonical = source.canonical_attributes or {}
            values[field] = canonical[field] if field in canonical else source.raw_attributes.get(field)
        applied, rejected = [], []
        for change in changes:
            if not set(members).intersection([change.source_feature_id, change.comparison_feature_id]):
                continue
            if change.status == "rejected":
                rejected.append(change.id)
            elif change.status in {"accepted", "validated"}:
                if change.change_type in {"missing", "ambiguous_identity", "split_merge"}:
                    failures.append({"change_id": change.id, "reason": "Manual membership/selection revision required"})
                    continue
                if change.boundary_change:
                    if not change.after_geometry:
                        failures.append({"change_id": change.id, "reason": "Boundary revision lacks after geometry"})
                        continue
                    geometry = change.after_geometry
                if change.after_attributes is not None:
                    values = change.after_attributes
                applied.append(change.id)
            else:
                failures.append({"change_id": change.id, "reason": "Unresolved change affects selected baseline"})
        applied_geometry_changes = []
        ancestor_ids = {selection.parcel_entity_id}
        for item in reversed(geometry_changes):
            if item.status == "approved" and ancestor_ids.intersection(item.successor_ids or []):
                ancestor_ids.update(item.predecessor_ids or [])
                applied_geometry_changes.append(item)
        for geometry_change in geometry_changes:
            affected_ids = set(geometry_change.parcel_entity_ids or []) | set(geometry_change.successor_ids or [])
            if selection.parcel_entity_id not in affected_ids:
                continue
            if geometry_change.status in {"draft", "deferred"}:
                failures.append({"geometry_change_set_id": geometry_change.id, "reason": "Unresolved geometry changeset affects selected baseline"})
            elif geometry_change.status == "approved":
                if selection.parcel_entity_id in (geometry_change.approved_geometries or {}):
                    geometry = geometry_change.approved_geometries[selection.parcel_entity_id]
                    if geometry_change not in applied_geometry_changes:
                        applied_geometry_changes.append(geometry_change)
                else:
                    failures.append({"geometry_change_set_id": geometry_change.id,
                                      "reason": "Approved geometry changeset has no geometry for the selected parcel"})
        if applied_geometry_changes:
            from .advanced import resolve_canonical_geometry
            from shapely.geometry import mapping as geometry_mapping
            resolved, _ = resolve_canonical_geometry(db, selection.parcel_entity_id)
            if resolved is not None:
                geometry = geometry_mapping(resolved)
        match_ids = sorted(m.id for m in matches if m.left_feature_id in members and m.right_feature_id in members)
        review_ids = sorted(r.id for r in reviews if (r.target_type == "match" and r.target_id in match_ids) or (r.target_type == "change" and r.target_id in applied + rejected))
        source_lineage = []
        for source_id in members:
            source = sources[source_id]
            dataset = datasets[source.dataset_id]
            mapping = next((m for m in mappings.values() if m.dataset_id == dataset.id and m.version == dataset.schema_mapping_version and m.status == "confirmed"), None)
            if mapping is None:
                failures.append({"dataset_id": dataset.id, "reason": "Confirmed schema mapping required"})
            source_lineage.append({"feature_id": source_id, "dataset_id": dataset.id, "sha256": dataset.content_hash,
                                   "schema_mapping_id": mapping.id if mapping else None,
                                   "schema_mapping": mapping.mapping if mapping else None,
                                   "crs_transformation": dataset.crs_transform,
                                   "capture_date": str(dataset.capture_date) if dataset.capture_date else None})
        lineage = {"parcel_entity_id": selection.parcel_entity_id, "source_feature_ids": members,
                   "sources": source_lineage, "accepted_match_proposal_ids": match_ids,
                   "matching_evidence": [{"id": m.id, "method": m.model_version, "evidence": m.evidence} for m in matches if m.id in match_ids],
                    "accepted_change_ids": applied, "rejected_change_ids": rejected, "review_decisions": review_ids,
                    "geometry_changes": [{"id": item.id, "revision": item.revision, "operation": item.operation,
                                          "predecessor_ids": item.predecessor_ids, "successor_ids": item.successor_ids,
                                          "before_geometries": item.before_geometries, "approved_geometries": item.approved_geometries,
                                          "measurements": item.measurements, "authorization": item.authorization,
                                          "approved_by": item.approved_by, "decision_at": item.decision_at.isoformat() if item.decision_at else None,
                                          "decision_rationale": item.decision_rationale} for item in applied_geometry_changes],
                    "selection": {"id": selection.id, "revision": selection.revision, "actor_id": selection.actor_id,
                                 "rationale": selection.rationale, "geometry_source_id": selection.geometry_source_id,
                                 "attribute_source_id": selection.attribute_source_id, "attribute_sources": selection.attribute_sources},
                   "crs_transformations": [s["crs_transformation"] for s in source_lineage if s["crs_transformation"]]}
        output.append({"parcel_entity_id": selection.parcel_entity_id, "source_feature_id": attributes.id,
                       "geometry": geometry, "attributes": values, "lineage": lineage})
    for source in sources.values():
        if source.id not in selected_ids:
            excluded.append({"source_feature_id": source.id, "dataset_id": source.dataset_id,
                             "reason": source.processing_reason or "No reviewer-approved baseline selection"})
    # Include every relevant input, even if a caller forgot to increment project revision.
    context = {"policy": policy, "features": output, "exclusions": sorted(excluded, key=lambda x: x["source_feature_id"]),
               "datasets": sorted((d.id, d.content_hash, d.declared_crs, d.schema_mapping_version) for d in datasets.values()),
               "reviews": [(r.id, r.decision, r.target_revision) for r in reviews],
                "changes": sorted((c.id, c.revision, c.status) for c in changes),
                "geometry_changes": sorted((c.id, c.revision, c.status, c.operation) for c in geometry_changes)}
    digest = hashlib.sha256(json.dumps(context, sort_keys=True, default=str).encode()).hexdigest()
    return output, excluded, failures, digest


def validate_project(db, project_id, actor_id):
    from .models import Project
    project = db.get(Project, project_id)
    policy = processing_policy(db, project_id)
    candidates, excluded, failures, digest = candidate_snapshot(db, project)
    geometries, parcel_ids = [], []
    analysis_crs = None
    for candidate in candidates:
        if candidate["geometry"] is None:
            continue
        try:
            geometry = shape(candidate["geometry"])
            check_coordinates(geometry, True)
            if not geometry.is_valid:
                raise ValueError("Invalid resulting geometry")
            if geometry.geom_type not in {"Polygon", "MultiPolygon"}:
                raise ValueError("Parcel publication requires Polygon or MultiPolygon geometry")
            from .services import analysis_crs_for
            analysis_crs = analysis_crs or analysis_crs_for(geometry)
            geometries.append(metric_geometry(geometry, analysis_crs))
            parcel_ids.append(candidate["parcel_entity_id"])
        except ValueError:
            failures.append({"parcel_entity_id": candidate["parcel_entity_id"],
                             "reason": "Candidate measurement failed. Check geometry, CRS and units."})
    tree = STRtree(geometries)
    neighborhoods = []
    for i, geometry in enumerate(geometries):
        for j in tree.query(geometry, predicate="intersects"):
            j = int(j)
            if j <= i:
                continue
            overlap = geometry.intersection(geometries[j]).area
            neighborhoods.append([parcel_ids[i], parcel_ids[j]])
            if overlap > policy["overlap_tolerance_m2"]:
                failures.append({"parcel_entity_id": parcel_ids[i], "neighbor_id": parcel_ids[j],
                                 "reason": "Resulting canonical parcels overlap", "overlap_m2": overlap})
    if not candidates:
        failures.append({"reason": "No reviewer-approved baseline selected"})
    report = {"valid": not failures, "candidate_hash": digest, "candidate_count": len(candidates),
              "project_revision": project.workflow_revision, "policy": {**POLICY, 'processing': policy}, "analysis_crs": analysis_crs,
              "neighbor_pairs_checked": neighborhoods, "failures": failures, "exclusions": excluded,
              "validated_changes": sum(len(c["lineage"]["accepted_change_ids"]) for c in candidates),
              "open_topology_errors": sum(1 for f in failures if "overlap_m2" in f)}
    project.validation_report = report
    project.validated_revision = project.workflow_revision if report["valid"] else None
    audit(db, "project_validated", actor_id, project_id, details=report)
    db.commit()
    return report


def publish(db, project, actor):
    candidates, excluded, failures, digest = candidate_snapshot(db, project)
    if failures:
        raise ValueError("Publication blocked: " + "; ".join(sorted({f["reason"] for f in failures})))
    report = project.validation_report or {}
    if not report.get("valid") or report.get("candidate_hash") != digest or project.validated_revision != project.workflow_revision:
        raise ValueError("Candidate revision changed or has not passed validation; validate again")
    lineage_manifest = {"policy": POLICY, "candidate_hash": digest, "validation_report": report,
                        "features": [c["lineage"] for c in candidates], "exclusions": excluded}
    output_context = canonical_output_context(candidates)
    version = PublishedVersion(project_id=project.id, version_number=next_version(db, project.id), created_by=actor.id,
                               project_revision=project.workflow_revision, validation_report=report,
                               excluded_records=excluded, lineage_manifest=lineage_manifest,
                               manifest_sha256=canonical_sha256(lineage_manifest), output_sha256=canonical_sha256(output_context))
    db.add(version)
    db.flush()
    for candidate in candidates:
        native = shape(candidate["geometry"]) if candidate["geometry"] else None
        db.add(PublicationFeature(version_id=version.id, **candidate, **spatial_column(native, "EPSG:4326")))
    audit(db, "published", actor.id, project.id, "published_version", version.id, {"candidate_hash": digest})
    db.commit()
    return version
