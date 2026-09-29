"""add cancelled task status

Them trang thai rieng 'Cancelled' cho DeployTask.status, tach biet voi
'Rejected': Cancelled danh cho nguoi tao task tu huy request cua chinh
minh (hoac Admin/Super Admin), con Rejected van gioi han o Admin tro len.

Revision ID: a36803c44cd4
Revises: 344649937cfe
Create Date: 2026-07-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a36803c44cd4'
down_revision: Union[str, Sequence[str], None] = '344649937cfe'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('deploy_task') as batch_op:
        batch_op.alter_column(
            'status',
            existing_type=sa.Enum('TASK', 'CONFIRMED', 'REJECTED', 'RUNNING', 'DONE', name='taskstatus'),
            type_=sa.Enum('TASK', 'CONFIRMED', 'REJECTED', 'CANCELLED', 'RUNNING', 'DONE', name='taskstatus'),
            existing_nullable=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    # Ve lai 'Rejected' truoc khi thu hep enum, tranh vi pham constraint moi.
    op.execute("UPDATE deploy_task SET status = 'REJECTED' WHERE status = 'CANCELLED'")

    with op.batch_alter_table('deploy_task') as batch_op:
        batch_op.alter_column(
            'status',
            existing_type=sa.Enum('TASK', 'CONFIRMED', 'REJECTED', 'CANCELLED', 'RUNNING', 'DONE', name='taskstatus'),
            type_=sa.Enum('TASK', 'CONFIRMED', 'REJECTED', 'RUNNING', 'DONE', name='taskstatus'),
            existing_nullable=False,
        )
