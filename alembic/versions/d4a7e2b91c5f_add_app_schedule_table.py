"""add app_schedule table + schedule permissions (menu Schedule: tu dong Restart / Tat-Bat app theo lich)

Them 4 cot quyen RIENG tren users (can_schedule_{restart,toggle}_{staging,production}) -
KHONG ke thua quyen Restart/Replicas cua menu Staging/Production.

Revision ID: d4a7e2b91c5f
Revises: 4e8e42e87792
Create Date: 2026-10-16 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd4a7e2b91c5f'
down_revision: Union[str, Sequence[str], None] = '4e8e42e87792'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_PERM_COLUMNS = (
    'can_schedule_restart_staging',
    'can_schedule_restart_production',
    'can_schedule_toggle_staging',
    'can_schedule_toggle_production',
)


def upgrade() -> None:
    """Upgrade schema."""
    for column in _PERM_COLUMNS:
        op.add_column('users', sa.Column(column, sa.Boolean(), nullable=False, server_default=sa.false()))
    with op.batch_alter_table('users') as batch_op:
        for column in _PERM_COLUMNS:
            batch_op.alter_column(column, server_default=None)
    op.create_table(
        'app_schedule',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('action', sa.String(length=20), nullable=False),
        sa.Column('env', sa.String(length=20), nullable=False),
        sa.Column('project', sa.String(length=255), nullable=False),
        sa.Column('application', sa.String(length=255), nullable=False),
        sa.Column('frequency', sa.String(length=20), nullable=False),
        sa.Column('time_of_day', sa.String(length=5), nullable=True),
        sa.Column('weekday', sa.Integer(), nullable=True),
        sa.Column('day_of_month', sa.Integer(), nullable=True),
        sa.Column('run_once_at', sa.DateTime(), nullable=True),
        sa.Column('next_run_at', sa.DateTime(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('last_run_at', sa.DateTime(), nullable=True),
        sa.Column('last_ok', sa.Boolean(), nullable=True),
        sa.Column('last_message', sa.Text(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('created_by_email', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_app_schedule_active_next_run', 'app_schedule', ['is_active', 'next_run_at'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_app_schedule_active_next_run', table_name='app_schedule')
    op.drop_table('app_schedule')
    for column in reversed(_PERM_COLUMNS):
        op.drop_column('users', column)
