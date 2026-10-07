"""Persist reviewed ranker activation state."""
from alembic import op
import sqlalchemy as sa

revision = "0008_ranker_activation"
down_revision = "0007_frozen_citizen_grants"
branch_labels = depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("model_artifacts")}
    if "activation_status" not in columns:
        op.add_column("model_artifacts", sa.Column("activation_status", sa.String(20), nullable=False, server_default="inactive"))
    if "activated_by" not in columns:
        op.add_column("model_artifacts", sa.Column("activated_by", sa.String(36), nullable=True))
    if "activated_at" not in columns:
        op.add_column("model_artifacts", sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    raise RuntimeError("Restore a backup to downgrade without losing model activation history")
