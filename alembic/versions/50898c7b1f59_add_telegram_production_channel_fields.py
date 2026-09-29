"""add telegram production channel fields

AppSetting.telegram_chat_id_production: chat id Telegram RIENG cho kenh Production
(doc lap voi telegram_chat_id_system dung cho luong deploy va telegram_chat_id_staging
dung cho Bat/Tat staging + Production replicas).
AppSetting.enable_staging_notify / enable_production_notify: cong tac BAT/TAT gui
thong bao Telegram rieng cho tung moi truong Staging / Production, mac dinh BAT.

Revision ID: 50898c7b1f59
Revises: 6a3e339e5c3a
Create Date: 2026-08-02 23:47:51.065352

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '50898c7b1f59'
down_revision: Union[str, Sequence[str], None] = '6a3e339e5c3a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'app_setting',
        sa.Column('telegram_chat_id_production', sa.String(length=255), nullable=False, server_default=''),
    )
    op.add_column(
        'app_setting',
        sa.Column('enable_staging_notify', sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        'app_setting',
        sa.Column('enable_production_notify', sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    with op.batch_alter_table('app_setting') as batch_op:
        batch_op.alter_column('telegram_chat_id_production', server_default=None)
        batch_op.alter_column('enable_staging_notify', server_default=None)
        batch_op.alter_column('enable_production_notify', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('app_setting', 'enable_production_notify')
    op.drop_column('app_setting', 'enable_staging_notify')
    op.drop_column('app_setting', 'telegram_chat_id_production')
