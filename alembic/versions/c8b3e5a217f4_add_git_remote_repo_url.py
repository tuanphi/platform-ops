"""add git remote repo url

Them git_remote_repo_url vao app_setting - phan URL repo Helm charts (khong chua
credential) sua duoc qua UI /settings/general. Credential rieng chuyen sang bien
moi GIT_REMOTE_CREDENTIALS trong .env (thay the GIT_REMOTE_APPLICATIONS_URL cu,
truoc day gop ca URL lan credential lam 1).

Revision ID: c8b3e5a217f4
Revises: a1c5f9d3e742
Create Date: 2026-07-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8b3e5a217f4'
down_revision: Union[str, Sequence[str], None] = 'a1c5f9d3e742'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'app_setting', sa.Column('git_remote_repo_url', sa.String(length=255), nullable=False, server_default='')
    )
    with op.batch_alter_table('app_setting') as batch_op:
        batch_op.alter_column('git_remote_repo_url', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('app_setting', 'git_remote_repo_url')
