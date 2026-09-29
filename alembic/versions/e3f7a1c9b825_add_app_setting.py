"""add app setting

Bang singleton app_setting (1 dong, id=1) luu cac gia tri cau hinh Super Admin
sua duoc tu UI /settings/general thay vi phai sua .env + restart app: auth
dev-mode/password login, thu muc/duong dan catalog, thoi gian cho phep
rollback, ArgoCD url/postfix, Telegram chat id he thong, bat/tat mail va
scheduler.

Revision ID: e3f7a1c9b825
Revises: b7e1c9a4d206
Create Date: 2026-07-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e3f7a1c9b825'
down_revision: Union[str, Sequence[str], None] = 'b7e1c9a4d206'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'app_setting',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('auth_dev_mode', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('auth_enable_password', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('catalog_charts_dir', sa.String(length=255), nullable=False, server_default='charts'),
        sa.Column('catalog_excluded_dirs', sa.Text(), nullable=False, server_default=''),
        sa.Column(
            'chart_values_file_template',
            sa.String(length=255),
            nullable=False,
            server_default='charts/{project}/{application}/values-production.yaml',
        ),
        sa.Column('rollback_allowed_seconds', sa.Integer(), nullable=False, server_default='259200'),
        sa.Column('argocd_url_api', sa.String(length=255), nullable=False, server_default=''),
        sa.Column('argocd_url_application', sa.String(length=255), nullable=False, server_default=''),
        sa.Column('argocd_application_postfix', sa.String(length=255), nullable=False, server_default=''),
        sa.Column('telegram_chat_id_system', sa.String(length=255), nullable=False, server_default=''),
        sa.Column('enable_mail', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('enable_scheduler', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('app_setting') as batch_op:
        for column in (
            'auth_dev_mode', 'auth_enable_password', 'catalog_charts_dir', 'catalog_excluded_dirs',
            'chart_values_file_template', 'rollback_allowed_seconds', 'argocd_url_api',
            'argocd_url_application', 'argocd_application_postfix', 'telegram_chat_id_system',
            'enable_mail', 'enable_scheduler',
        ):
            batch_op.alter_column(column, server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('app_setting')
