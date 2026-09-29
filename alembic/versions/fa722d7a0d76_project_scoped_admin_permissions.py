"""project scoped admin permissions

Bo role toan cuc 'admin'. Quyen Confirm/Reject + Run gio duoc Super Admin
gan truc tiep cho tung user theo tung Group (project) qua bang
group_admin_access, thay vi mot role co dinh.

Revision ID: fa722d7a0d76
Revises: 4b46103ecf92
Create Date: 2026-07-27 02:31:46.068339

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fa722d7a0d76'
down_revision: Union[str, Sequence[str], None] = '4b46103ecf92'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Role 'admin' cu khong con ton tai -> ha ve 'user' (an toan, super admin
    # se gan lai quyen cu the theo tung project qua group_admin_access sau).
    # Luu y: SQLAlchemy Enum(Role) luu TEN member (vd 'ADMIN'), khong phai .value
    # (vd 'admin') - phai dung dung chuoi viet hoa nay thi UPDATE moi khop du lieu that.
    op.execute("UPDATE users SET role = 'USER' WHERE role = 'ADMIN'")

    with op.batch_alter_table('users') as batch_op:
        batch_op.alter_column(
            'role',
            existing_type=sa.Enum('USER', 'ADMIN', 'SUPER_ADMIN', name='role'),
            type_=sa.Enum('USER', 'SUPER_ADMIN', name='role'),
            existing_nullable=False,
        )

    op.create_table(
        'group_admin_access',
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['group_id'], ['groups.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('user_id', 'group_id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('group_admin_access')

    with op.batch_alter_table('users') as batch_op:
        batch_op.alter_column(
            'role',
            existing_type=sa.Enum('USER', 'SUPER_ADMIN', name='role'),
            type_=sa.Enum('USER', 'ADMIN', 'SUPER_ADMIN', name='role'),
            existing_nullable=False,
        )
