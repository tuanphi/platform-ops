"""add queued task status and queue fields

Tinh nang hang doi Run/Rollback (worker nen xu ly tuan tu, xem app/scheduler.py::
job_process_task_queue + app/services/task_service.py::process_task_queue): bam Run/
Rollback gio chi CAS task sang trang thai QUEUED va tra loi ngay, khong con giu git lock/
thread request cho toi khi push git xong nhu truoc - fix loi that trong production (Run
nhieu task gan nhau -> cac task sau bi GitError "khong lay duoc git lock" do
_GIT_LOCK_TIMEOUT_SECONDS = 2s qua ngan cho 1 lan push that su).

3 thay doi schema:
1. Them gia tri 'QUEUED' vao enum deploy_task.status (MySQL native ENUM, chi CO THEM,
   khong doi/xoa gia tri nao khac nen chi can 1 buoc ALTER, khong can 3-buoc "mo rong ->
   migrate du lieu -> thu hep" nhu vd e0776cb5a420_rename_confirmed_status_to_approved).
2. Them cot deploy_task.queued_from_status (cung kieu enum taskstatus, nullable) - worker
   dung de revert dung trang thai truoc do (Task/Approved) neu git that bai sau khi task
   da CAS sang RUNNING.
3. Them cot deploy_task.rollback_target_task_id (Integer, nullable, KHONG co FK constraint
   - xem docstring DeployTask.rollback_target_task_id trong models.py ve ly do) - danh dau
   1 dong la task duoc tao boi Rollback (worker se goi perform_rollback thay vi perform_run).

Revision ID: b1f4a6d29c37
Revises: 2826f4f8d9cd
Create Date: 2026-08-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1f4a6d29c37'
down_revision: Union[str, Sequence[str], None] = '2826f4f8d9cd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATUS_VALUES_WITHOUT_QUEUED = ('TASK', 'APPROVED', 'REJECTED', 'CANCELLED', 'RUNNING', 'DONE')
_STATUS_VALUES_WITH_QUEUED = ('TASK', 'APPROVED', 'REJECTED', 'CANCELLED', 'QUEUED', 'RUNNING', 'DONE')


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('deploy_task') as batch_op:
        batch_op.alter_column(
            'status',
            existing_type=sa.Enum(*_STATUS_VALUES_WITHOUT_QUEUED, name='taskstatus'),
            type_=sa.Enum(*_STATUS_VALUES_WITH_QUEUED, name='taskstatus'),
            existing_nullable=False,
        )
        batch_op.add_column(
            sa.Column('queued_from_status', sa.Enum(*_STATUS_VALUES_WITH_QUEUED, name='taskstatus'), nullable=True)
        )
        batch_op.add_column(sa.Column('rollback_target_task_id', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('deploy_task') as batch_op:
        batch_op.drop_column('rollback_target_task_id')
        batch_op.drop_column('queued_from_status')

    # Khong the con dong nao dang QUEUED khi downgrade (tinh nang hang doi bi go bo) -
    # dua ve TASK de an toan voi constraint moi (giong tinh than downgrade cua
    # a36803c44cd4_add_cancelled_task_status), tranh vi pham constraint enum sau khi thu hep.
    op.execute("UPDATE deploy_task SET status = 'TASK' WHERE status = 'QUEUED'")

    with op.batch_alter_table('deploy_task') as batch_op:
        batch_op.alter_column(
            'status',
            existing_type=sa.Enum(*_STATUS_VALUES_WITH_QUEUED, name='taskstatus'),
            type_=sa.Enum(*_STATUS_VALUES_WITHOUT_QUEUED, name='taskstatus'),
            existing_nullable=False,
        )
