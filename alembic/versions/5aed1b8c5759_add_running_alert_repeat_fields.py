"""add running alert repeat fields

AppSetting.running_alert_repeat_minutes: khoang cach toi thieu (phut) giua 2 lan canh
bao lap lai cho CUNG 1 task RUNNING qua lau (0 = chi canh bao 1 lan duy nhat, khong lap
lai) - sua duoc qua UI /settings/general (section "rollback", card "Task"), dung chung
voi DeployTask.last_alert_at (thoi diem canh bao gan nhat cho 1 task) trong
app/scheduler.py::job_check_running_too_long de giam spam Telegram.

Revision ID: 5aed1b8c5759
Revises: b1f4a6d29c37
Create Date: 2026-08-14 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5aed1b8c5759'
down_revision: Union[str, Sequence[str], None] = 'b1f4a6d29c37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'app_setting',
        sa.Column('running_alert_repeat_minutes', sa.Integer(), nullable=False, server_default='30'),
    )
    with op.batch_alter_table('app_setting') as batch_op:
        batch_op.alter_column('running_alert_repeat_minutes', server_default=None)

    op.add_column(
        'deploy_task',
        sa.Column('last_alert_at', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('deploy_task', 'last_alert_at')
    op.drop_column('app_setting', 'running_alert_repeat_minutes')
