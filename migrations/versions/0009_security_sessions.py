"""Preserve accounts and add session versioning and revocation."""
from alembic import op
import sqlalchemy as sa

revision = "0009_security_sessions"
down_revision = "0008_ranker_activation"
branch_labels = depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "auth_version" not in {column["name"] for column in inspector.get_columns("users")}:
        op.add_column("users", sa.Column("auth_version", sa.Integer(), nullable=False, server_default="0"))
    if "revoked_tokens" not in inspector.get_table_names():
        op.create_table("revoked_tokens", sa.Column("jti", sa.String(36), primary_key=True),
                        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
                        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
        op.create_index("ix_revoked_tokens_user_id", "revoked_tokens", ["user_id"])
        op.create_index("ix_revoked_tokens_expires_at", "revoked_tokens", ["expires_at"])


def downgrade():
    raise RuntimeError("Restore a backup to downgrade without losing session revocation history")
