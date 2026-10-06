"""Bounded, permissioned raster source registry."""
from alembic import op
import sqlalchemy as sa

revision = "0005_raster_assets"
down_revision = "0004_review_workflows"
branch_labels = depends_on = None


def upgrade():
    if "raster_assets" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table("raster_assets", sa.Column("id", sa.String(36), primary_key=True),
                        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True),
                        sa.Column("name", sa.String(200), nullable=False), sa.Column("raw_path", sa.Text(), nullable=False),
                        sa.Column("content_hash", sa.String(64), nullable=False, index=True),
                        sa.Column("mime_type", sa.String(120), nullable=False, server_default="image/tiff"),
                        sa.Column("source_crs", sa.String(120)), sa.Column("bounds", sa.JSON()),
                        sa.Column("metadata", sa.JSON(), nullable=False, server_default="{}"),
                        sa.Column("attribution", sa.String(500)), sa.Column("access_classification", sa.String(30), nullable=False, server_default="internal"),
                        sa.Column("source_date", sa.Date()), sa.Column("status", sa.String(30), nullable=False, server_default="registered"),
                        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
                        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    raise RuntimeError("Restore a backup to downgrade without losing raster provenance")
