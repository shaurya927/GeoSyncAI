"""Additive metadata, reviewed evidence, fieldwork and compliance workflows."""
from alembic import op
import sqlalchemy as sa

revision = "0004_review_workflows"
down_revision = "0003_attribute_sources"
branch_labels = depends_on = None


def _json(name, default="{}", nullable=False):
    return sa.Column(name, sa.JSON(), nullable=nullable, server_default=default)


def _fk(name, table, nullable=False, index=True):
    return sa.Column(name, sa.String(36), sa.ForeignKey(f"{table}.id", ondelete="CASCADE" if table in {"projects", "datasets", "field_assignments"} else None), nullable=nullable, index=index)


def _table(name, *columns, constraints=()):
    op.create_table(name, sa.Column("id", sa.String(36), primary_key=True), *columns, *constraints)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    dataset_columns = {
        "administrative_namespace": sa.Column("administrative_namespace", sa.JSON(), nullable=False, server_default="{}"),
        "license_classification": sa.Column("license_classification", sa.String(120), nullable=True),
        "provenance": sa.Column("provenance", sa.JSON(), nullable=False, server_default="{}"),
        "source_version": sa.Column("source_version", sa.String(120), nullable=True),
        "version_label": sa.Column("version_label", sa.String(120), nullable=True),
    }
    existing = {column["name"] for column in inspector.get_columns("datasets")}
    for name, column in dataset_columns.items():
        if name not in existing:
            op.add_column("datasets", column)
    job_columns = {
        "stage": sa.Column("stage", sa.String(60), nullable=True),
        "progress": sa.Column("progress", sa.JSON(), nullable=False, server_default="{}"),
        "heartbeat_at": sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        "warnings": sa.Column("warnings", sa.JSON(), nullable=False, server_default="[]"),
        "cancellation_requested": sa.Column("cancellation_requested", sa.Boolean(), nullable=False, server_default="false"),
    }
    existing = {column["name"] for column in inspector.get_columns("jobs")}
    for name, column in job_columns.items():
        if name not in existing:
            op.add_column("jobs", column)

    tables = set(inspector.get_table_names())
    if "mapping_dictionary_entries" not in tables:
        _table("mapping_dictionary_entries", _fk("project_id", "projects"),
               sa.Column("canonical_field", sa.String(80), nullable=False),
               sa.Column("source_term", sa.String(255), nullable=False),
               sa.Column("language", sa.String(10), nullable=False, server_default="en"),
               sa.Column("normalized_term", sa.String(255), nullable=False),
               sa.Column("value_type", sa.String(40)), sa.Column("units", sa.String(80)),
               sa.Column("cardinality", sa.String(30), nullable=False, server_default="single"),
               sa.Column("rationale", sa.Text(), nullable=False), sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
               sa.Column("status", sa.String(30), nullable=False, server_default="draft"), _fk("created_by", "users"),
               sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    if "department_templates" not in tables:
        _table("department_templates", _fk("project_id", "projects"),
               sa.Column("department", sa.String(200), nullable=False), sa.Column("name", sa.String(200), nullable=False),
               _json("administrative_namespace"), sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
               _json("mapping"), sa.Column("field_descriptions", sa.JSON(), nullable=False, server_default="[]"),
               sa.Column("status", sa.String(30), nullable=False, server_default="draft"), _fk("created_by", "users"),
               sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    if "reconciliation_cases" not in tables:
        _table("reconciliation_cases", _fk("project_id", "projects"), _fk("anchor_feature_id", "source_features", True),
               sa.Column("source_feature_ids", sa.JSON(), nullable=False, server_default="[]"),
               sa.Column("status", sa.String(40), nullable=False, server_default="proposed"), sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
               _json("recommendation"), _json("competing_values"), sa.Column("evidence", sa.JSON(), nullable=False, server_default="[]"),
               sa.Column("rationale", sa.Text()), _fk("created_by", "users"), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    if "geometry_change_sets" not in tables:
        _table("geometry_change_sets", _fk("project_id", "projects"), sa.Column("operation", sa.String(30), nullable=False),
               sa.Column("status", sa.String(30), nullable=False, server_default="draft"), sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
               sa.Column("parcel_entity_ids", sa.JSON(), nullable=False, server_default="[]"), sa.Column("predecessor_ids", sa.JSON(), nullable=False, server_default="[]"),
               sa.Column("successor_ids", sa.JSON(), nullable=False, server_default="[]"), _json("before_geometries"), _json("draft_geometries"),
               _json("approved_geometries"), _json("measurements"), _json("authorization"), sa.Column("rationale", sa.Text(), nullable=False),
               _fk("created_by", "users"), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    if "ground_control_sessions" not in tables:
        _table("ground_control_sessions", _fk("project_id", "projects"), _fk("dataset_id", "datasets"),
               sa.Column("method", sa.String(40), nullable=False), sa.Column("control_points", sa.JSON(), nullable=False, server_default="[]"),
               _json("residuals"), sa.Column("status", sa.String(30), nullable=False, server_default="draft"), sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
               _fk("approved_by", "users", True), _fk("created_by", "users"), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    if "training_examples" not in tables:
        _table("training_examples", _fk("project_id", "projects"), _fk("proposal_id", "match_proposals", True),
               sa.Column("label", sa.Integer(), nullable=False), _json("features"), sa.Column("group_key", sa.String(255), nullable=False),
               sa.Column("split", sa.String(30), nullable=False, server_default="train"), sa.Column("hard_negative", sa.Boolean(), nullable=False, server_default="false"),
               _fk("created_by", "users"), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    if "model_artifacts" not in tables:
        _table("model_artifacts", _fk("project_id", "projects"), sa.Column("model_type", sa.String(50), nullable=False),
               sa.Column("version", sa.String(100), nullable=False), _json("artifact"), _json("metrics"),
               sa.Column("dataset_fingerprint", sa.String(64), nullable=False), sa.Column("seed", sa.Integer(), nullable=False, server_default="0"),
               _fk("created_by", "users"), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    if "field_assignments" not in tables:
        _table("field_assignments", _fk("project_id", "projects"), _fk("assignee_id", "users"),
               sa.Column("parcel_entity_ids", sa.JSON(), nullable=False, server_default="[]"), sa.Column("expires_at", sa.DateTime(timezone=True)),
               sa.Column("status", sa.String(30), nullable=False, server_default="assigned"), sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
               _json("reference_policy"), _fk("created_by", "users"), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    if "field_evidence" not in tables:
        _table("field_evidence", _fk("assignment_id", "field_assignments"), _fk("parcel_entity_id", "parcel_entities"),
               sa.Column("client_event_id", sa.String(100), nullable=False, unique=True), _json("payload"),
               sa.Column("status", sa.String(30), nullable=False, server_default="submitted"), sa.Column("expected_project_revision", sa.Integer(), nullable=False),
               _fk("created_by", "users"), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    if "compliance_rules" not in tables:
        _table("compliance_rules", _fk("project_id", "projects"), sa.Column("name", sa.String(200), nullable=False),
               sa.Column("jurisdiction", sa.String(200), nullable=False), sa.Column("category", sa.String(80), nullable=False),
               sa.Column("version", sa.Integer(), nullable=False, server_default="1"), sa.Column("effective_from", sa.Date()), sa.Column("effective_to", sa.Date()),
               sa.Column("inputs", sa.JSON(), nullable=False, server_default="[]"), _json("formula"), _json("threshold"),
               sa.Column("status", sa.String(30), nullable=False, server_default="draft"), _fk("created_by", "users"), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    if "citizen_grants" not in tables:
        _table("citizen_grants", _fk("project_id", "projects"), _fk("citizen_id", "users"), _fk("parcel_entity_id", "parcel_entities"),
               sa.Column("fields", sa.JSON(), nullable=False, server_default="[]"), sa.Column("expires_at", sa.DateTime(timezone=True)),
               sa.Column("status", sa.String(30), nullable=False, server_default="active"), _fk("granted_by", "users"), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    if "citizen_cases" not in tables:
        _table("citizen_cases", _fk("project_id", "projects"), _fk("grant_id", "citizen_grants"), _fk("parcel_entity_id", "parcel_entities"),
               sa.Column("category", sa.String(50), nullable=False), sa.Column("description", sa.Text(), nullable=False),
               sa.Column("status", sa.String(30), nullable=False, server_default="submitted"), sa.Column("response", sa.Text()),
               sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))


def downgrade() -> None:
    raise RuntimeError("Restore a backup to downgrade without losing reviewed workflow history")
