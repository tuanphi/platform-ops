"""add configurable restart cooldown setting

AppSetting.restart_cooldown_seconds: cooldown (giay) giua 2 lan Restart CUNG 1 app,
truoc day la hang so cung _RESTART_COOLDOWN_SECONDS = 30.0 trong
app/services/restart_service.py - gio chuyen thanh setting sua duoc qua UI
Settings > General > ArgoCD, mac dinh nang len 60 (tu 30) theo yeu cau thuc te
2026-08-18.

Revision ID: 0bfe28d8ca6e
Revises: 98603c005d2c
Create Date: 2026-08-18 16:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0bfe28d8ca6e'
down_revision: Union[str, Sequence[str], None] = '98603c005d2c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'app_setting',
        sa.Column('restart_cooldown_seconds', sa.Integer(), nullable=False, server_default='60'),
    )
    with op.batch_alter_table('app_setting') as batch_op:
        batch_op.alter_column('restart_cooldown_seconds', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('app_setting', 'restart_cooldown_seconds')
