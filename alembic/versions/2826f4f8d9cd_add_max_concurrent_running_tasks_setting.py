"""add max concurrent running tasks setting

AppSetting.max_concurrent_running_tasks: so task duoc phep o trang thai Running CUNG
LUC tren toan he thong (0 = KHONG gioi han, giu nguyen hanh vi cu). Sua duoc qua UI
/settings/general (section "rollback"), ap dung o tang service cho MOI duong tao task
Running: Run don le, bulk Run, Rollback, scheduler auto-deploy - xem
app/services/task_service.py.

Revision ID: 2826f4f8d9cd
Revises: 7a2b4c9e1d3f
Create Date: 2026-08-12 19:34:50.557521

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2826f4f8d9cd'
down_revision: Union[str, Sequence[str], None] = '7a2b4c9e1d3f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'app_setting',
        sa.Column('max_concurrent_running_tasks', sa.Integer(), nullable=False, server_default='0'),
    )
    with op.batch_alter_table('app_setting') as batch_op:
        batch_op.alter_column('max_concurrent_running_tasks', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('app_setting', 'max_concurrent_running_tasks')
