"""add restart log table

Bang audit-log cho tinh nang nut "Restart" o /staging va /production - moi lan bam ghi
1 dong (du thanh cong hay that bai), xem app/models.py::RestartLog va
app/services/restart_service.py.

Revision ID: 32d0f9545b8b
Revises: 5aed1b8c5759
Create Date: 2026-08-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '32d0f9545b8b'
down_revision: Union[str, Sequence[str], None] = '5aed1b8c5759'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'restart_log',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('env', sa.String(length=20), nullable=False),
        sa.Column('project', sa.String(length=255), nullable=False),
        sa.Column('application', sa.String(length=255), nullable=False),
        sa.Column('workload_kind', sa.String(length=50), nullable=True),
        sa.Column('workload_name', sa.String(length=255), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('user_email', sa.String(length=255), nullable=False),
        sa.Column('ok', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('restart_log') as batch_op:
        batch_op.alter_column('ok', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('restart_log')
