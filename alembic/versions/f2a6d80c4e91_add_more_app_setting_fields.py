"""add more app setting fields

Them 7 field vao app_setting de Super Admin sua duoc tu UI /settings/general:
git commit author email/name, mail from/to/subject, enable_30min_reminder,
running_alert_after_minutes. Cac field con lai (Google OAuth callback, git
remote/branch/directory) van chi doc tu .env vi rui ro cao neu sua sai qua UI
(xem trao doi ve rui ro luc them tinh nang nay).

Revision ID: f2a6d80c4e91
Revises: e3f7a1c9b825
Create Date: 2026-07-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2a6d80c4e91'
down_revision: Union[str, Sequence[str], None] = 'e3f7a1c9b825'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'app_setting',
        sa.Column('enable_30min_reminder', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        'app_setting',
        sa.Column('running_alert_after_minutes', sa.Integer(), nullable=False, server_default='15'),
    )
    op.add_column(
        'app_setting',
        sa.Column(
            'git_commit_author_email', sa.String(length=255), nullable=False, server_default='form-deploy@g-pay.vn'
        ),
    )
    op.add_column(
        'app_setting',
        sa.Column(
            'git_commit_author_name', sa.String(length=255), nullable=False, server_default='form-deploy@g-pay.vn'
        ),
    )
    op.add_column(
        'app_setting',
        sa.Column('mail_from', sa.String(length=255), nullable=False, server_default='form-deploy@g-pay.vn'),
    )
    op.add_column('app_setting', sa.Column('mail_to', sa.Text(), nullable=False, server_default=''))
    op.add_column(
        'app_setting',
        sa.Column(
            'mail_subject',
            sa.String(length=255),
            nullable=False,
            server_default='[form-deploy] Deploy notification',
        ),
    )
    with op.batch_alter_table('app_setting') as batch_op:
        for column in (
            'enable_30min_reminder', 'running_alert_after_minutes', 'git_commit_author_email',
            'git_commit_author_name', 'mail_from', 'mail_to', 'mail_subject',
        ):
            batch_op.alter_column(column, server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    for column in (
        'mail_subject', 'mail_to', 'mail_from', 'git_commit_author_name', 'git_commit_author_email',
        'running_alert_after_minutes', 'enable_30min_reminder',
    ):
        op.drop_column('app_setting', column)
