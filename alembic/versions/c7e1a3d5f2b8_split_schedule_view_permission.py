"""split Schedule View into Restart view / Tat-Bat view (per env)

Thay users.can_schedule_view_{staging,production} (1 quyen xem chung 2 tab) bang
can_schedule_restart_view_{env} + can_schedule_toggle_view_{env}. User dang co quyen View
cu duoc chuyen sang CA 2 quyen view moi (khong mat quyen xem).

Revision ID: c7e1a3d5f2b8
Revises: b5d2e8f4c1a9
Create Date: 2026-10-17 17:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c7e1a3d5f2b8'
down_revision: Union[str, Sequence[str], None] = 'b5d2e8f4c1a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ENVS = ('staging', 'production')
_NEW = [f'can_schedule_{kind}_view_{env}' for kind in ('restart', 'toggle') for env in _ENVS]
_OLD = [f'can_schedule_view_{env}' for env in _ENVS]


def upgrade() -> None:
    """Upgrade schema."""
    for column in _NEW:
        op.add_column('users', sa.Column(column, sa.Boolean(), nullable=False, server_default=sa.false()))
    for env in _ENVS:
        op.execute(
            f"UPDATE users SET can_schedule_restart_view_{env} = can_schedule_view_{env}, "
            f"can_schedule_toggle_view_{env} = can_schedule_view_{env}"
        )
    with op.batch_alter_table('users') as batch_op:
        for column in _NEW:
            batch_op.alter_column(column, server_default=None)
        for column in _OLD:
            batch_op.drop_column(column)


def downgrade() -> None:
    """Downgrade schema."""
    for column in _OLD:
        op.add_column('users', sa.Column(column, sa.Boolean(), nullable=False, server_default=sa.false()))
    for env in _ENVS:
        op.execute(
            f"UPDATE users SET can_schedule_view_{env} = 1 "
            f"WHERE can_schedule_restart_view_{env} = 1 OR can_schedule_toggle_view_{env} = 1"
        )
    with op.batch_alter_table('users') as batch_op:
        for column in _OLD:
            batch_op.alter_column(column, server_default=None)
        for column in _NEW:
            batch_op.drop_column(column)
