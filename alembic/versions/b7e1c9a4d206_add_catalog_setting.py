"""add catalog setting

Bang singleton catalog_setting (1 dong, id=1) luu cau hinh runtime cho catalog -
truoc mat chi co auto_sync_enabled, phuc vu tinh nang bat/tat auto sync
project/application tu UI /catalog ma khong can restart app.

Revision ID: b7e1c9a4d206
Revises: cc3784da9c90
Create Date: 2026-07-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e1c9a4d206'
down_revision: Union[str, Sequence[str], None] = 'cc3784da9c90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'catalog_setting',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('auto_sync_enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('catalog_setting') as batch_op:
        batch_op.alter_column('auto_sync_enabled', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('catalog_setting')
