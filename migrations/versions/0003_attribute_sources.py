"""Per-field reviewed attribute precedence."""
from alembic import op
import sqlalchemy as sa
revision = '0003_attribute_sources'
down_revision = '0002_reviewed_baselines'
branch_labels = depends_on = None


def upgrade():
    op.add_column('parcel_selections', sa.Column('attribute_sources', sa.JSON(), nullable=False, server_default='{}'))


def downgrade():
    raise RuntimeError('Restore a backup to downgrade without losing selection history')
