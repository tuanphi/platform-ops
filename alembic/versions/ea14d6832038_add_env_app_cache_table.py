"""add env app cache table

Bang cache danh sach app + replicas hien tai cho tinh nang Bat/Tat staging + thay doi
replicas Production - dong bo tu git (quet charts/{group}/{application}/ tren nhanh
staging/master + doc replicas tu file values), GIONG HET co che catalog Group/Project
(dong bo dinh ky + nut "Lam moi" thu cong), thay vi fetch git moi lan load trang.

Revision ID: ea14d6832038
Revises: 3603e28d3a19
Create Date: 2026-08-01 18:04:48.457780

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ea14d6832038'
down_revision: Union[str, Sequence[str], None] = '3603e28d3a19'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'env_app_cache',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('env', sa.String(length=20), nullable=False),
        sa.Column('project', sa.String(length=255), nullable=False),
        sa.Column('application', sa.String(length=255), nullable=False),
        sa.Column('replicas', sa.String(length=20), nullable=False, server_default='0'),
        sa.Column('synced_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('env', 'project', 'application', name='uq_env_app_cache_env_project_app'),
    )
    with op.batch_alter_table('env_app_cache') as batch_op:
        batch_op.alter_column('replicas', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('env_app_cache')
