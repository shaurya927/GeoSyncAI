"""Bootstrap the current SQLAlchemy model and support additive upgrades."""
from alembic import op
import sqlalchemy as sa

from migrations.baseline_schema import baseline_metadata

revision = "0001_bootstrap"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Frozen v1 definitions support clean databases and additive upgrades of
    # the original prototype without importing mutable application models.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    baseline_metadata(bind.dialect.name == "postgresql").create_all(bind=bind)
    inspector = sa.inspect(bind)
    additions = {
        "projects": [
            ("workflow_revision", sa.Integer(), {"server_default": "0"}),
            ("validated_revision", sa.Integer(), {"nullable": True}),
            ("validation_report", sa.JSON(), {"nullable": True}),
        ],
        "datasets": [
            ("analysis_crs", sa.String(120), {"nullable": True}),
            ("crs_transform", sa.JSON(), {"nullable": True}),
            ("crs_confirmed_by", sa.String(36), {"nullable": True}),
            ("crs_confirmed_at", sa.DateTime(timezone=True), {"nullable": True}),
            ("access_classification", sa.String(30), {"server_default": "internal"}),
            ("accuracy_metadata", sa.JSON(), {"server_default": "{}"}),
            ("schema_mapping_version", sa.Integer(), {"server_default": "0"}),
        ],
        "source_features": [("administrative_context", sa.JSON(), {"server_default": "{}"})],
        "parcel_entities": [("status", sa.String(30), {"server_default": "active"})],
        "match_proposals": [("revision", sa.Integer(), {"server_default": "1"})],
        "topology_conflicts": [("revision", sa.Integer(), {"server_default": "1"})],
        "change_proposals": [("revision", sa.Integer(), {"server_default": "1"}),
                              ("affected_neighbors", sa.JSON(), {"server_default": "[]"})],
        "review_decisions": [
            ("expected_revision", sa.Integer(), {"nullable": True}),
            ("target_revision", sa.Integer(), {"server_default": "1"}),
        ],
        "published_versions": [
            ("project_revision", sa.Integer(), {"server_default": "0"}),
            ("base_version_id", sa.String(36), {"nullable": True}),
            ("excluded_records", sa.JSON(), {"server_default": "[]"}),
            ("validation_report", sa.JSON(), {"nullable": True}),
        ],
        "jobs": [
            ("configuration_hash", sa.String(64), {"nullable": True}),
            ("attempts", sa.Integer(), {"server_default": "0"}),
        ],
    }
    for table_name, columns in additions.items():
        existing = {column["name"] for column in inspector.get_columns(table_name)}
        for name, column_type, kwargs in columns:
            if name not in existing:
                op.add_column(table_name, sa.Column(name, column_type, **kwargs))


def downgrade() -> None:
    # Prototype deployments retain data during downgrade; destructive drops
    # are intentionally excluded from the migration.
    pass
