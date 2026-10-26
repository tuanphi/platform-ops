"""add app_schedule.last_skipped + app_replicas_restore (bo qua lich Tat/Bat khi app da o
dung trang thai; nho so pod truoc khi tat de bat lai dung so pod cu - dung chung cho
/staging, /production va /schedule)

Revision ID: a8c3f1d2b7e4
Revises: d4a7e2b91c5f
Create Date: 2026-10-17 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a8c3f1d2b7e4'
down_revision: Union[str, Sequence[str], None] = 'd4a7e2b91c5f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('app_schedule', sa.Column('last_skipped', sa.Boolean(), nullable=False, server_default=sa.false()))
    with op.batch_alter_table('app_schedule') as batch_op:
        batch_op.alter_column('last_skipped', server_default=None)

    op.create_table(
        'app_replicas_restore',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('env', sa.String(length=20), nullable=False),
        sa.Column('project', sa.String(length=255), nullable=False),
        sa.Column('application', sa.String(length=255), nullable=False),
        sa.Column('replicas', sa.Integer(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('env', 'project', 'application', name='uq_app_replicas_restore_app'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('app_replicas_restore')
    op.drop_column('app_schedule', 'last_skipped')
