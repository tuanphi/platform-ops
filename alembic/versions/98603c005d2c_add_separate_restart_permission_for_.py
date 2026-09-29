"""add separate restart permission for staging and production

User.can_toggle_restart_staging / can_toggle_restart_production: quyen RIENG cho nut
Restart o /staging va /production, tach hoan toan khoi can_toggle_staging /
can_toggle_production (quyen sua replicas - hien thi tren UI la "Staging Replicas"/
"Production Replicas", KHONG con bao gom Restart nua). Hien thi tren UI la "Staging
Restart"/"Production Restart". Xem User.can_restart_staging/can_restart_production/
can_access_staging trong app/models.py.

Revision ID: 98603c005d2c
Revises: 8167583b1fff
Create Date: 2026-08-18 15:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '98603c005d2c'
down_revision: Union[str, Sequence[str], None] = '8167583b1fff'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'users',
        sa.Column('can_toggle_restart_staging', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        'users',
        sa.Column('can_toggle_restart_production', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    with op.batch_alter_table('users') as batch_op:
        batch_op.alter_column('can_toggle_restart_staging', server_default=None)
        batch_op.alter_column('can_toggle_restart_production', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'can_toggle_restart_production')
    op.drop_column('users', 'can_toggle_restart_staging')
