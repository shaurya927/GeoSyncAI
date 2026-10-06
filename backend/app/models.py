from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4
from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, JSON
from sqlalchemy.orm import Mapped, mapped_column
from .config import get_settings
from .db import Base

POSTGIS = get_settings().database_url.startswith("postgresql")
if POSTGIS:
    from geoalchemy2 import Geometry


def uid() -> str:
    return str(uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(30), default="viewer")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    workflow_revision: Mapped[int] = mapped_column(Integer, default=0)
    validated_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    validation_report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProjectMember(Base):
    __tablename__ = "project_members"
    __table_args__ = (UniqueConstraint("project_id", "user_id", name="uq_project_member"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_role: Mapped[str] = mapped_column(String(30), default="member")


class Dataset(Base):
    __tablename__ = "datasets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    source_organization: Mapped[str | None] = mapped_column(String(200), nullable=True)
    capture_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    raw_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    declared_crs: Mapped[str | None] = mapped_column(String(120), nullable=True)
    normalized_crs: Mapped[str | None] = mapped_column(String(120), nullable=True)
    analysis_crs: Mapped[str | None] = mapped_column(String(120), nullable=True)
    crs_transform: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    crs_confirmed_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    crs_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    access_classification: Mapped[str] = mapped_column(String(30), default="internal")
    accuracy_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    schema_mapping_version: Mapped[int] = mapped_column(Integer, default=0)
    geometry_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="registered")
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    normalized_count: Mapped[int] = mapped_column(Integer, default=0)
    validation_report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    parent_dataset_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    administrative_namespace: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    license_classification: Mapped[str | None] = mapped_column(String(120), nullable=True)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_version: Mapped[str | None] = mapped_column(String(120), nullable=True)
    version_label: Mapped[str | None] = mapped_column(String(120), nullable=True)


class SourceFeature(Base):
    __tablename__ = "source_features"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    original_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    raw_attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    original_geometry: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    normalized_geometry: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    administrative_context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    canonical_attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    if POSTGIS:
        spatial_geometry: Mapped[Any | None] = mapped_column(
            Geometry(geometry_type="GEOMETRY", srid=4326, spatial_index=True), nullable=True
        )
    geometry_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="processed")
    processing_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ParcelEntity(Base):
    __tablename__ = "parcel_entities"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    canonical_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ParcelSelection(Base):
    """Explicit reviewer-approved baseline, separate from identity decisions."""
    __tablename__ = "parcel_selections"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    parcel_entity_id: Mapped[str] = mapped_column(ForeignKey("parcel_entities.id"), unique=True)
    geometry_source_id: Mapped[str | None] = mapped_column(ForeignKey("source_features.id"), nullable=True)
    attribute_source_id: Mapped[str] = mapped_column(ForeignKey("source_features.id"))
    attribute_sources: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    rationale: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ParcelSourceLink(Base):
    __tablename__ = "parcel_source_links"
    __table_args__ = (UniqueConstraint("parcel_entity_id", "source_feature_id", name="uq_parcel_source_link"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    parcel_entity_id: Mapped[str] = mapped_column(ForeignKey("parcel_entities.id", ondelete="CASCADE"), index=True)
    source_feature_id: Mapped[str] = mapped_column(ForeignKey("source_features.id", ondelete="CASCADE"), index=True)
    match_proposal_id: Mapped[str | None] = mapped_column(ForeignKey("match_proposals.id"), nullable=True)
    link_status: Mapped[str] = mapped_column(String(30), default="accepted")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SchemaMapping(Base):
    __tablename__ = "schema_mappings"
    __table_args__ = (UniqueConstraint("dataset_id", "version", name="uq_dataset_schema_mapping_version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    mapping: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_fields: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MatchProposal(Base):
    __tablename__ = "match_proposals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    left_feature_id: Mapped[str | None] = mapped_column(ForeignKey("source_features.id"), nullable=True)
    right_feature_id: Mapped[str | None] = mapped_column(ForeignKey("source_features.id"), nullable=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    score_type: Mapped[str] = mapped_column(String(40), default="uncalibrated_rule_score")
    status: Mapped[str] = mapped_column(String(40), default="proposed")
    candidate_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model_version: Mapped[str] = mapped_column(String(80), default="rules-v1")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TopologyConflict(Base):
    __tablename__ = "topology_conflicts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    dataset_id: Mapped[str | None] = mapped_column(ForeignKey("datasets.id"), nullable=True)
    feature_id: Mapped[str | None] = mapped_column(ForeignKey("source_features.id"), nullable=True)
    conflict_type: Mapped[str] = mapped_column(String(60))
    severity: Mapped[str] = mapped_column(String(20), default="warning")
    description: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="open")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChangeProposal(Base):
    __tablename__ = "change_proposals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    change_type: Mapped[str] = mapped_column(String(50))
    source_feature_id: Mapped[str | None] = mapped_column(ForeignKey("source_features.id"), nullable=True)
    comparison_feature_id: Mapped[str | None] = mapped_column(ForeignKey("source_features.id"), nullable=True)
    before_geometry: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after_geometry: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    before_attributes: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after_attributes: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    area_delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    affected_neighbors: Mapped[list[str]] = mapped_column(JSON, default=list)
    boundary_change: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(40), default="proposed")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReviewDecision(Base):
    __tablename__ = "review_decisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    target_type: Mapped[str] = mapped_column(String(40))
    target_id: Mapped[str] = mapped_column(String(36), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    decision: Mapped[str] = mapped_column(String(40))
    rationale: Mapped[str] = mapped_column(Text)
    expected_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PublishedVersion(Base):
    __tablename__ = "published_versions"
    __table_args__ = (UniqueConstraint("project_id", "version_number", name="uq_project_version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(30), default="published")
    project_revision: Mapped[int] = mapped_column(Integer, default=0)
    base_version_id: Mapped[str | None] = mapped_column(ForeignKey("published_versions.id"), nullable=True)
    excluded_records: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    validation_report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    lineage_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PublicationFeature(Base):
    __tablename__ = "publication_features"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    version_id: Mapped[str] = mapped_column(ForeignKey("published_versions.id", ondelete="CASCADE"), index=True)
    source_feature_id: Mapped[str] = mapped_column(ForeignKey("source_features.id"))
    parcel_entity_id: Mapped[str | None] = mapped_column(ForeignKey("parcel_entities.id"), nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    geometry: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    if POSTGIS:
        spatial_geometry: Mapped[Any | None] = mapped_column(
            Geometry(geometry_type="GEOMETRY", srid=4326, spatial_index=True), nullable=True
        )
    lineage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("project_id", "idempotency_key", name="uq_project_job_idempotency"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    job_type: Mapped[str] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    configuration_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stage: Mapped[str | None] = mapped_column(String(60), nullable=True)
    progress: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    cancellation_requested: Mapped[bool] = mapped_column(Boolean, default=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(100))
    target_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MappingDictionaryEntry(Base):
    """A reviewed bilingual semantic mapping; terms never silently become identifiers."""
    __tablename__ = "mapping_dictionary_entries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    canonical_field: Mapped[str] = mapped_column(String(80), index=True)
    source_term: Mapped[str] = mapped_column(String(255))
    language: Mapped[str] = mapped_column(String(10), default="en")
    normalized_term: Mapped[str] = mapped_column(String(255))
    value_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    units: Mapped[str | None] = mapped_column(String(80), nullable=True)
    cardinality: Mapped[str] = mapped_column(String(30), default="single")
    rationale: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DepartmentTemplate(Base):
    """Reusable, versioned source schema template with explicit departmental scope."""
    __tablename__ = "department_templates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    department: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(200))
    administrative_namespace: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    mapping: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    field_descriptions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReconciliationCase(Base):
    """Explicit multi-source evidence bundle; membership and selected geometry stay separate."""
    __tablename__ = "reconciliation_cases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    anchor_feature_id: Mapped[str | None] = mapped_column(ForeignKey("source_features.id"), nullable=True)
    source_feature_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(40), default="proposed")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    recommendation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    competing_values: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GeometryChangeSet(Base):
    """Reviewed geometry operation with a draft/approved boundary and lineage."""
    __tablename__ = "geometry_change_sets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    operation: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30), default="draft")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    parcel_entity_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    predecessor_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    successor_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    before_geometries: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    draft_geometries: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    approved_geometries: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    measurements: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    authorization: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    rationale: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GroundControlSession(Base):
    __tablename__ = "ground_control_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    method: Mapped[str] = mapped_column(String(40))
    control_points: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    residuals: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    approved_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TrainingExample(Base):
    __tablename__ = "training_examples"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    proposal_id: Mapped[str | None] = mapped_column(ForeignKey("match_proposals.id"), nullable=True)
    label: Mapped[int] = mapped_column(Integer)
    features: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    group_key: Mapped[str] = mapped_column(String(255))
    split: Mapped[str] = mapped_column(String(30), default="train")
    hard_negative: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelArtifact(Base):
    __tablename__ = "model_artifacts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    model_type: Mapped[str] = mapped_column(String(50))
    version: Mapped[str] = mapped_column(String(100))
    artifact: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    dataset_fingerprint: Mapped[str] = mapped_column(String(64))
    seed: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FieldAssignment(Base):
    __tablename__ = "field_assignments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    assignee_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    parcel_entity_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="assigned")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    reference_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FieldEvidence(Base):
    __tablename__ = "field_evidence"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    assignment_id: Mapped[str] = mapped_column(ForeignKey("field_assignments.id", ondelete="CASCADE"), index=True)
    parcel_entity_id: Mapped[str] = mapped_column(ForeignKey("parcel_entities.id"), index=True)
    client_event_id: Mapped[str] = mapped_column(String(100), unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="submitted")
    expected_project_revision: Mapped[int] = mapped_column(Integer)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ComplianceRule(Base):
    __tablename__ = "compliance_rules"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    jurisdiction: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(80))
    version: Mapped[int] = mapped_column(Integer, default=1)
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    inputs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    formula: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    threshold: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RasterAsset(Base):
    __tablename__ = "raster_assets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    raw_path: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    mime_type: Mapped[str] = mapped_column(String(120), default="image/tiff")
    source_crs: Mapped[str | None] = mapped_column(String(120), nullable=True)
    bounds: Mapped[list[float] | None] = mapped_column(JSON, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    attribution: Mapped[str | None] = mapped_column(String(500), nullable=True)
    access_classification: Mapped[str] = mapped_column(String(30), default="internal")
    source_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="registered")
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CitizenGrant(Base):
    __tablename__ = "citizen_grants"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    citizen_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    parcel_entity_id: Mapped[str] = mapped_column(ForeignKey("parcel_entities.id"), index=True)
    fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="active")
    granted_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CitizenCase(Base):
    __tablename__ = "citizen_cases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    grant_id: Mapped[str] = mapped_column(ForeignKey("citizen_grants.id"), index=True)
    parcel_entity_id: Mapped[str] = mapped_column(ForeignKey("parcel_entities.id"), index=True)
    category: Mapped[str] = mapped_column(String(50))
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="submitted")
    response: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
