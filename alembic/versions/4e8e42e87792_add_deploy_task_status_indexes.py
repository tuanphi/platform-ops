"""add deploy_task indexes for status/id and project+application+status

Bang deploy_task tich luy lich su deploy (chi INSERT, gan nhu khong DELETE) nen ngay
cang lon, trong khi worker hang doi (task_service._promote_queue_candidate_ids, goi tu
scheduler.job_process_task_queue) MOI 5 GIAY filter WHERE status='Queued' ORDER BY id
ASC - cong them nhieu job scheduler khac (job_check_running_done moi 1 phut, job_
remind_approved_now moi 1 phut, job_check_running_too_long moi 10 phut, job_auto_deploy
moi 1 phut) va dashboard /home (7 query COUNT theo status moi lan load trang) deu filter
theo cot status. Cot nay KHONG co index tu truoc (xem 4b46103ecf92_initial_schema) nen
moi lan goi la 1 lan full table scan tren MySQL - anh huong ro nhat o worker chay 5s/lan.

Them 2 index:
  - (status, id): dung dung cho query worker hang doi (filter + ORDER BY id cung luc) va
    cac job scheduler filter theo status don le.
  - (project, application, status): ho tro check trung commit luc tao task
    (task_service.create_task) va list_rollback_candidates (project+application+
    status=Done, order by id desc limit 5).

Bang deploy_task tich luy chu yeu status "cuoi" (Done/Rejected/Cancelled), cac status
"dang hoat dong" (Queued/Running/Approved) ma cac job tren quan tam chi la so nho - index
rat chon loc, gia re ve dung luong so voi loi ich giam full scan lien tuc.

Revision ID: 4e8e42e87792
Revises: 0bfe28d8ca6e
Create Date: 2026-08-19 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '4e8e42e87792'
down_revision: Union[str, Sequence[str], None] = '0bfe28d8ca6e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index('ix_deploy_task_status_id', 'deploy_task', ['status', 'id'])
    op.create_index(
        'ix_deploy_task_project_application_status',
        'deploy_task',
        ['project', 'application', 'status'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_deploy_task_project_application_status', table_name='deploy_task')
    op.drop_index('ix_deploy_task_status_id', table_name='deploy_task')
