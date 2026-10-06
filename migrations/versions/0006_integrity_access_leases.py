"""Integrity checkpoints, job leases and controlled alignment metadata."""
from alembic import op
import sqlalchemy as sa

revision = "0006_integrity_access_leases"
down_revision = "0005_raster_assets"
branch_labels = depends_on = None


def add_if_missing(table: str, name: str, column: sa.Column) -> None:
    inspector = sa.inspect(op.get_bind())
    if name not in {item["name"] for item in inspector.get_columns(table)}:
        op.add_column(table, column)


def upgrade() -> None:
    for name, column in {
        "manifest_sha256": sa.Column("manifest_sha256", sa.String(64), nullable=True),
        "output_sha256": sa.Column("output_sha256", sa.String(64), nullable=True),
        "signature": sa.Column("signature", sa.Text(), nullable=True),
    }.items():
        add_if_missing("published_versions", name, column)
    for name, column in {
        "owner_token": sa.Column("owner_token", sa.String(80), nullable=True),
        "lease_expires_at": sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        "max_attempts": sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        "cancelled_by": sa.Column("cancelled_by", sa.String(36), nullable=True),
    }.items():
        add_if_missing("jobs", name, column)
    for name, column in {
        "approved_by": sa.Column("approved_by", sa.String(36), nullable=True),
        "decision_at": sa.Column("decision_at", sa.DateTime(timezone=True), nullable=True),
        "decision_rationale": sa.Column("decision_rationale", sa.Text(), nullable=True),
    }.items():
        add_if_missing("geometry_change_sets", name, column)
    for name, column in {
        "source_crs": sa.Column("source_crs", sa.String(120), nullable=True),
        "target_crs": sa.Column("target_crs", sa.String(120), nullable=True),
        "approved_dataset_id": sa.Column("approved_dataset_id", sa.String(36), nullable=True),
    }.items():
        add_if_missing("ground_control_sessions", name, column)


def downgrade() -> None:
    raise RuntimeError("Restore a backup to downgrade without losing integrity and lease history")
