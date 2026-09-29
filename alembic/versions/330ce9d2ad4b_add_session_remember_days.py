"""add session_remember_days

Them session_remember_days vao app_setting - so ngay song cua cookie session khi
user tick "Ghi nho dang nhap", sua duoc qua UI /settings/general (gioi han doc tu
SESSION_REMEMBER_MIN_DAYS/MAX_DAYS trong .env, validate server-side o
settings_router.update_general) thay vi chi doc tu .env + restart nhu truoc. Gia tri
duoc doc lai tai thoi diem set cookie trong app.auth.RememberMeMiddleware nen doi qua
UI co hieu luc ngay, khong can restart app.

Revision ID: 330ce9d2ad4b
Revises: c8b3e5a217f4
Create Date: 2026-07-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '330ce9d2ad4b'
down_revision: Union[str, Sequence[str], None] = 'c8b3e5a217f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'app_setting', sa.Column('session_remember_days', sa.Integer(), nullable=False, server_default='7')
    )
    with op.batch_alter_table('app_setting') as batch_op:
        batch_op.alter_column('session_remember_days', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('app_setting', 'session_remember_days')
