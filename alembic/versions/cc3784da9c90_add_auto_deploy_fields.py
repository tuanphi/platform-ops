"""add auto deploy fields

Them auto_deploy (bat/tat) va auto_run_at (gio tu dong Run, luu UTC) cho DeployTask,
phuc vu tinh nang auto deploy: khi bat va den gio hen, scheduler tu dong Run task
thay vi chi nhac nho nhu confirmed_run_at.

Revision ID: cc3784da9c90
Revises: a36803c44cd4
Create Date: 2026-07-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cc3784da9c90'
down_revision: Union[str, Sequence[str], None] = 'a36803c44cd4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('deploy_task', sa.Column('auto_deploy', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('deploy_task', sa.Column('auto_run_at', sa.DateTime(), nullable=True))
    with op.batch_alter_table('deploy_task') as batch_op:
        batch_op.alter_column('auto_deploy', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('deploy_task', 'auto_run_at')
    op.drop_column('deploy_task', 'auto_deploy')
