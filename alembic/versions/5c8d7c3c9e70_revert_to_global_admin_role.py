"""revert to global admin role

Bo co che gan quyen Admin theo tung Group (group_admin_access). Quay lai
model 3 role co dinh: user / admin (confirm+reject toan bo, khong Run,
khong vao settings) / super_admin (toan quyen).

Luu y: cac user tung bi ha tu 'ADMIN' xuong 'USER' o migration truoc
(fa722d7a0d76) se KHONG tu dong duoc phuc hoi lai 'ADMIN' o day - can
Super Admin tu tay set lai role cho dung user can thiet sau khi migrate.

Revision ID: 5c8d7c3c9e70
Revises: fa722d7a0d76
Create Date: 2026-07-27 02:43:04.405105

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5c8d7c3c9e70'
down_revision: Union[str, Sequence[str], None] = 'fa722d7a0d76'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_table('group_admin_access')

    with op.batch_alter_table('users') as batch_op:
        batch_op.alter_column(
            'role',
            existing_type=sa.Enum('USER', 'SUPER_ADMIN', name='role'),
            type_=sa.Enum('USER', 'ADMIN', 'SUPER_ADMIN', name='role'),
            existing_nullable=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    # Ha 'ADMIN' ve 'USER' truoc khi thu hep lai enum, tranh vi pham constraint moi.
    op.execute("UPDATE users SET role = 'USER' WHERE role = 'ADMIN'")

    with op.batch_alter_table('users') as batch_op:
        batch_op.alter_column(
            'role',
            existing_type=sa.Enum('USER', 'ADMIN', 'SUPER_ADMIN', name='role'),
            type_=sa.Enum('USER', 'SUPER_ADMIN', name='role'),
            existing_nullable=False,
        )

    op.create_table('group_admin_access',
    sa.Column('user_id', sa.INTEGER(), nullable=False),
    sa.Column('group_id', sa.INTEGER(), nullable=False),
    sa.ForeignKeyConstraint(['group_id'], ['groups.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('user_id', 'group_id')
    )
