"""add users.can_schedule_view_{staging,production} (quyen chi xem menu Schedule)

Revision ID: b5d2e8f4c1a9
Revises: a8c3f1d2b7e4
Create Date: 2026-10-17 15:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b5d2e8f4c1a9'
down_revision: Union[str, Sequence[str], None] = 'a8c3f1d2b7e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = ('can_schedule_view_staging', 'can_schedule_view_production')


def upgrade() -> None:
    """Upgrade schema."""
    for column in _COLUMNS:
        op.add_column('users', sa.Column(column, sa.Boolean(), nullable=False, server_default=sa.false()))
    with op.batch_alter_table('users') as batch_op:
        for column in _COLUMNS:
            batch_op.alter_column(column, server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    for column in reversed(_COLUMNS):
        op.drop_column('users', column)
