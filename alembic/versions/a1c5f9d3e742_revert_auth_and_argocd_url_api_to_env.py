"""revert auth and argocd url_api to env-only

Xoa 3 cot khoi app_setting: auth_dev_mode, auth_enable_password, argocd_url_api.
Sau khi danh gia lai rui ro, 3 gia tri nay quay ve chi doc tu .env (khong con
sua duoc qua UI /settings/general):
- AUTH_DEV_MODE/AUTH_ENABLE_PASSWORD la cong tac xac thuc, khong phai config
  van hanh thong thuong.
- ARGOCD_URL_API neu sua duoc qua UI se thanh kenh ro ri ARGOCD_TOKEN (van la
  secret trong .env) toi bat ky URL nao duoc dien vao.

Revision ID: a1c5f9d3e742
Revises: f2a6d80c4e91
Create Date: 2026-07-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c5f9d3e742'
down_revision: Union[str, Sequence[str], None] = 'f2a6d80c4e91'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column('app_setting', 'auth_dev_mode')
    op.drop_column('app_setting', 'auth_enable_password')
    op.drop_column('app_setting', 'argocd_url_api')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        'app_setting', sa.Column('auth_dev_mode', sa.Boolean(), nullable=False, server_default=sa.true())
    )
    op.add_column(
        'app_setting', sa.Column('auth_enable_password', sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column(
        'app_setting', sa.Column('argocd_url_api', sa.String(length=255), nullable=False, server_default='')
    )
    with op.batch_alter_table('app_setting') as batch_op:
        for column in ('auth_dev_mode', 'auth_enable_password', 'argocd_url_api'):
            batch_op.alter_column(column, server_default=None)
