"""add production use merge request setting

AppSetting.production_use_merge_request: khi BAT, thay doi replicas Production khong
push thang len git_branch_master ma tao Merge Request (GitLab push options) tro ve nhanh
do, cho nguoi duyet thu cong. Mac dinh TAT (push thang nhu cu). Chi anh huong Production,
Staging khong co tuy chon nay.

Revision ID: 6a3e339e5c3a
Revises: ea14d6832038
Create Date: 2026-08-01 23:22:04.700734

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6a3e339e5c3a'
down_revision: Union[str, Sequence[str], None] = 'ea14d6832038'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'app_setting',
        sa.Column('production_use_merge_request', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    with op.batch_alter_table('app_setting') as batch_op:
        batch_op.alter_column('production_use_merge_request', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('app_setting', 'production_use_merge_request')
