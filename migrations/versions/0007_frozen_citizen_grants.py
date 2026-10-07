"""Bind citizen grants to frozen published versions."""
from alembic import op
import sqlalchemy as sa

revision = "0007_frozen_citizen_grants"
down_revision = "0006_integrity_access_leases"
branch_labels = depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("citizen_grants")}
    if "published_version_id" not in columns:
        op.add_column("citizen_grants", sa.Column("published_version_id", sa.String(36), nullable=True))
        op.create_index("ix_citizen_grants_published_version_id", "citizen_grants", ["published_version_id"])


def downgrade() -> None:
    raise RuntimeError("Restore a backup to downgrade without losing grant provenance")
