"""Explicit selected baselines and confirmed semantic attributes."""
from alembic import op
import sqlalchemy as sa

revision = "0002_reviewed_baselines"
down_revision = "0001_bootstrap"
branch_labels = depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "canonical_attributes" not in {c["name"] for c in inspector.get_columns("source_features")}:
        op.add_column("source_features", sa.Column("canonical_attributes", sa.JSON(), nullable=False, server_default="{}"))
    if not inspector.has_table("parcel_selections"):
        op.create_table("parcel_selections",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("parcel_entity_id", sa.String(36), sa.ForeignKey("parcel_entities.id"), nullable=False, unique=True),
            sa.Column("geometry_source_id", sa.String(36), sa.ForeignKey("source_features.id")),
            sa.Column("attribute_source_id", sa.String(36), sa.ForeignKey("source_features.id"), nullable=False),
            sa.Column("actor_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("rationale", sa.Text(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    raise RuntimeError("Data-preserving forward migrations only; restore a backup to downgrade")
