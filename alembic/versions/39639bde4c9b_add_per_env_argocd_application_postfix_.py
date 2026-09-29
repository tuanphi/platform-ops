"""add per-env argocd application postfix for restart

AppSetting.argocd_application_postfix_staging / _production: hau to ten Application
ArgoCD RIENG cho nut Restart o /staging va /production (xem
app/services/argocd_service.py::restart_workload). Tach biet voi
AppSetting.argocd_application_postfix cu (van giu nguyen, chi dung cho tinh nang
Running/DeployTask trong scheduler.py) - dung chung 1 hau to cho ca 2 moi truong lam
nut Restart Staging va Restart Production cung tac dong vao 1 ArgoCD Application (bug
thuc te gap phai tren production, incident 2026-08-18).

Revision ID: 39639bde4c9b
Revises: 32d0f9545b8b
Create Date: 2026-08-18 12:38:12.432209

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '39639bde4c9b'
down_revision: Union[str, Sequence[str], None] = '32d0f9545b8b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'app_setting',
        sa.Column('argocd_application_postfix_staging', sa.String(length=255), nullable=False, server_default=''),
    )
    op.add_column(
        'app_setting',
        sa.Column('argocd_application_postfix_production', sa.String(length=255), nullable=False, server_default=''),
    )
    with op.batch_alter_table('app_setting') as batch_op:
        batch_op.alter_column('argocd_application_postfix_staging', server_default=None)
        batch_op.alter_column('argocd_application_postfix_production', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('app_setting', 'argocd_application_postfix_production')
    op.drop_column('app_setting', 'argocd_application_postfix_staging')
