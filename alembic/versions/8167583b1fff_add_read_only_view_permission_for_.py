"""add read-only view permission for staging and production

User.can_view_staging / can_view_production: quyen CHI XEM (read-only) rieng cho
Staging/Production, doc lap voi can_toggle_staging / can_toggle_production (quyen GHI -
duoc doi ten hien thi tren UI tu "Replicas Staging"/"Replicas Production" thanh
"Staging Write"/"Production Write"). User co can_view_* (khong co can_toggle_*) vao xem
duoc trang nhung khong sua duoc gi (khong Bat/Tat, khong doi replicas, khong Restart,
khong Sync, khong Tat tat ca) - xem User.can_access_staging/can_write_staging trong
app/models.py.

Revision ID: 8167583b1fff
Revises: 39639bde4c9b
Create Date: 2026-08-18 14:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8167583b1fff'
down_revision: Union[str, Sequence[str], None] = '39639bde4c9b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'users',
        sa.Column('can_view_staging', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        'users',
        sa.Column('can_view_production', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    with op.batch_alter_table('users') as batch_op:
        batch_op.alter_column('can_view_staging', server_default=None)
        batch_op.alter_column('can_view_production', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'can_view_production')
    op.drop_column('users', 'can_view_staging')
