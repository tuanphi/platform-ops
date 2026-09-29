"""add app_setting git branch fields

Them git_branch_staging/git_branch_master vao app_setting - ten nhanh git dung
cho repo staging/master (git_service.ensure_staging_repo/ensure_master_repo),
sua duoc qua UI /settings/general (section "git") thay vi chi doc tu .env
(Settings.git_branch_staging/git_branch_master) + restart nhu truoc.

Revision ID: e6e1cd4983b6
Revises: 330ce9d2ad4b
Create Date: 2026-07-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e6e1cd4983b6'
down_revision: Union[str, Sequence[str], None] = '330ce9d2ad4b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'app_setting', sa.Column('git_branch_staging', sa.String(length=255), nullable=False, server_default='staging')
    )
    op.add_column(
        'app_setting', sa.Column('git_branch_master', sa.String(length=255), nullable=False, server_default='master')
    )
    with op.batch_alter_table('app_setting') as batch_op:
        batch_op.alter_column('git_branch_staging', server_default=None)
        batch_op.alter_column('git_branch_master', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('app_setting', 'git_branch_master')
    op.drop_column('app_setting', 'git_branch_staging')
