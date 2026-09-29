"""add staging toggle feature fields

Them cot cho tinh nang Bat/Tat staging + thay doi replicas Production:
- users.can_toggle_staging / can_toggle_production: cong tac TONG do Super Admin cap
  qua Settings > Users, cho phep Admin/User thuong dung 2 tinh nang nay (doc lap nhau).
- app_setting.staging_values_file_path / production_values_file_path: duong dan file
  YAML dung chung nhieu app (cau truc {project}: {application}: replicas: "N"), sua
  duoc qua UI /settings/general.
- app_setting.telegram_chat_id_staging: chat id Telegram RIENG cho thong bao Bat/Tat
  staging + Production replicas (dung chung 1 bot/chat cho ca 2, doc lap voi luong
  deploy) - bot token la secret, chi cau hinh qua .env (TELEGRAM_STAGING_BOT_TOKEN).

Revision ID: 3603e28d3a19
Revises: e0776cb5a420
Create Date: 2026-08-01 16:49:37.862009

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3603e28d3a19'
down_revision: Union[str, Sequence[str], None] = 'e0776cb5a420'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('users', sa.Column('can_toggle_staging', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('users', sa.Column('can_toggle_production', sa.Boolean(), nullable=False, server_default=sa.false()))
    with op.batch_alter_table('users') as batch_op:
        batch_op.alter_column('can_toggle_staging', server_default=None)
        batch_op.alter_column('can_toggle_production', server_default=None)

    op.add_column(
        'app_setting',
        sa.Column(
            'staging_values_file_path',
            sa.String(length=255),
            nullable=False,
            server_default='envValues/values-sandbox.yaml',
        ),
    )
    op.add_column(
        'app_setting',
        sa.Column(
            'production_values_file_path',
            sa.String(length=255),
            nullable=False,
            server_default='envValues/values-production.yaml',
        ),
    )
    op.add_column(
        'app_setting', sa.Column('telegram_chat_id_staging', sa.String(length=255), nullable=False, server_default='')
    )
    with op.batch_alter_table('app_setting') as batch_op:
        batch_op.alter_column('staging_values_file_path', server_default=None)
        batch_op.alter_column('production_values_file_path', server_default=None)
        batch_op.alter_column('telegram_chat_id_staging', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('app_setting', 'telegram_chat_id_staging')
    op.drop_column('app_setting', 'production_values_file_path')
    op.drop_column('app_setting', 'staging_values_file_path')
    op.drop_column('users', 'can_toggle_production')
    op.drop_column('users', 'can_toggle_staging')
