"""rename confirmed status to approved

Doi ten trang thai TaskStatus.CONFIRMED -> TaskStatus.APPROVED (chi doi TEN thanh vien
enum/gia tri hien thi "Confirmed" -> "Approved", KHONG doi cot DB nao khac - cac cot
`confirmed_run_at`, `confirmed_at`, `maintainer_confirmed` giu nguyen ten vi chi la du
lieu phu tro, khong phai gia tri trang thai chinh).

3 buoc de an toan voi MySQL (khong duoc xoa 1 gia tri enum truoc khi moi dong du lieu
dang dung gia tri do da duoc cap nhat - lam nguoc thu tu se gay loi "Data truncated" hoac
mat du lieu am tham cho cac dong con dang o CONFIRMED tai thoi diem ALTER):
  1. Mo rong enum de chua CA HAI 'CONFIRMED' va 'APPROVED' cung luc.
  2. UPDATE du lieu: moi dong dang CONFIRMED -> APPROVED.
  3. Thu hep enum lai, bo 'CONFIRMED' (khong con dong nao dung toi).

Revision ID: e0776cb5a420
Revises: e6e1cd4983b6
Create Date: 2026-07-31 21:19:56.376320

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e0776cb5a420'
down_revision: Union[str, Sequence[str], None] = 'e6e1cd4983b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('deploy_task') as batch_op:
        batch_op.alter_column(
            'status',
            existing_type=sa.Enum('TASK', 'CONFIRMED', 'REJECTED', 'CANCELLED', 'RUNNING', 'DONE', name='taskstatus'),
            type_=sa.Enum('TASK', 'CONFIRMED', 'APPROVED', 'REJECTED', 'CANCELLED', 'RUNNING', 'DONE', name='taskstatus'),
            existing_nullable=False,
        )

    op.execute("UPDATE deploy_task SET status = 'APPROVED' WHERE status = 'CONFIRMED'")

    with op.batch_alter_table('deploy_task') as batch_op:
        batch_op.alter_column(
            'status',
            existing_type=sa.Enum('TASK', 'CONFIRMED', 'APPROVED', 'REJECTED', 'CANCELLED', 'RUNNING', 'DONE', name='taskstatus'),
            type_=sa.Enum('TASK', 'APPROVED', 'REJECTED', 'CANCELLED', 'RUNNING', 'DONE', name='taskstatus'),
            existing_nullable=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('deploy_task') as batch_op:
        batch_op.alter_column(
            'status',
            existing_type=sa.Enum('TASK', 'APPROVED', 'REJECTED', 'CANCELLED', 'RUNNING', 'DONE', name='taskstatus'),
            type_=sa.Enum('TASK', 'CONFIRMED', 'APPROVED', 'REJECTED', 'CANCELLED', 'RUNNING', 'DONE', name='taskstatus'),
            existing_nullable=False,
        )

    op.execute("UPDATE deploy_task SET status = 'CONFIRMED' WHERE status = 'APPROVED'")

    with op.batch_alter_table('deploy_task') as batch_op:
        batch_op.alter_column(
            'status',
            existing_type=sa.Enum('TASK', 'CONFIRMED', 'APPROVED', 'REJECTED', 'CANCELLED', 'RUNNING', 'DONE', name='taskstatus'),
            type_=sa.Enum('TASK', 'CONFIRMED', 'REJECTED', 'CANCELLED', 'RUNNING', 'DONE', name='taskstatus'),
            existing_nullable=False,
        )
