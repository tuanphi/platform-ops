"""Xac nhan ban vá graceful GitError trong `task_service.create_task` va
`task_service.rollback_task` (xem app/services/task_service.py + app/services/git_service.py).

LICH SU (giu lai de tra cuu): ban dau 2 test duoi day duoc viet voi
`pytest.mark.xfail(strict=True)` de document 1 gap - `create_task` (goi
`git_service.check_commit_on_staging`) va `rollback_task` (goi
`git_service.perform_rollback`) KHONG co try/except quanh GitError, khac voi
`run_task` (co try/except + tra ve ServiceResult(ok=False, ...) than thien).
GitError (vd fail-fast lock timeout MASTER/STAGING 2s khi tranh chap) tung lan
thang ra ngoai, xuyen qua router (khong co try/except o tasks_router.create/
actions_router.rollback) thanh loi 500 chua duoc xu ly.

Developer da fix: ca 2 ham gio bọc try/except Exception quanh loi goi git_service
tuong ung, log bang `logger.exception` roi tra ve ServiceResult(ok=False,
message=...) than thien - dung truoc `db.add(...)` nen khong tao ban ghi DB rac
khi co loi. 2 test duoi day XAC NHAN hanh vi DUNG SAU FIX (khong con xfail) va
BO SUNG assert khong co ban ghi DB rac / task nguon khong bi doi trang thai."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.models import DeployTask, Role, TaskStatus
from app.services import git_service, task_service
from tests.conftest import make_user
from tests.test_task_service_lifecycle import make_group, make_project, make_task


def test_rollback_task_gracefully_handles_git_lock_timeout(db_session, monkeypatch):
    """DỜI SANG TẦNG WORKER: từ khi có hàng đợi, rollback_task() chỉ còn INSERT dòng
    QUEUED (không còn gọi perform_rollback đồng bộ - xem TaskStatus.QUEUED), nên GitError
    lock-timeout giờ chỉ có thể nổi lên từ run_queued_task() (worker) khi tới lượt task
    Rollback này thực sự chạy. Theo docstring _revert_or_fail_queued_task: khác Run
    thường (revert về status cũ), 1 task Rollback thất bại phải được đánh dấu REJECTED
    kèm lý do (không có "trạng thái trước đó" hợp lệ để quay về vì dòng này MỚI được
    INSERT riêng cho lần rollback)."""
    user = make_user(db_session, email="gap-rb@example.com", role=Role.SUPER_ADMIN)

    task = make_task(db_session, status=TaskStatus.DONE, image_version="v2", done_at=datetime.utcnow())
    target = make_task(db_session, status=TaskStatus.DONE, image_version="v1", commitid="def5678")

    task_count_before = db_session.query(DeployTask).count()

    enqueue_result = task_service.rollback_task(db_session, task.id, target.id, user)
    assert enqueue_result.ok is True
    rollback_row_id = enqueue_result.task.id
    assert db_session.query(DeployTask).count() == task_count_before + 1

    def raise_lock_timeout(*a, **k):
        raise git_service.GitError(
            "Hệ thống đang bận xử lý một thao tác Git khác trên 'master' "
            "(Run/Rollback/Sync catalog), vui lòng thử lại sau ít giây"
        )

    monkeypatch.setattr(task_service.git_service, "perform_rollback", raise_lock_timeout)

    # SAU FIX: GitError phai duoc bat lai o worker, tra ve ServiceResult(ok=False, message
    # chua noi dung loi than thien) thay vi lan thang ra ngoai thanh exception chua xu ly
    # (se lam chet job_process_task_queue neu khong duoc bat, xem app/scheduler.py).
    worker_result = task_service.run_queued_task(db_session, rollback_row_id)
    assert worker_result.ok is False
    assert "đang bận" in worker_result.message

    # Khong duoc tao them ban ghi DeployTask nao khac (khong co ban ghi rac ngoai dong
    # rollback_row da INSERT o buoc enqueue).
    assert db_session.query(DeployTask).count() == task_count_before + 1

    # Dong Rollback that bai phai duoc danh dau REJECTED kem ly do (khong con "trang thai
    # truoc do" hop le de quay ve, xem docstring _revert_or_fail_queued_task) - KHONG duoc
    # ket lai vinh vien o RUNNING.
    rollback_row = db_session.get(DeployTask, rollback_row_id)
    assert rollback_row.status == TaskStatus.REJECTED
    assert "Rollback thất bại" in (rollback_row.updatefor or "")

    # Task nguon (task) khong bi sua doi status (rollback_task/worker khong dong den task
    # nguon, chi dong moi tao rieng).
    db_session.refresh(task)
    assert task.status == TaskStatus.DONE


def test_create_task_gracefully_handles_git_lock_timeout(db_session, monkeypatch):
    user = make_user(db_session, email="gap-create@example.com", role=Role.SUPER_ADMIN)
    group = make_group(db_session)
    make_project(db_session, group, application="api")

    task_count_before = db_session.query(DeployTask).count()

    def raise_lock_timeout(*a, **k):
        raise git_service.GitError(
            "Hệ thống đang bận xử lý một thao tác Git khác trên 'staging' "
            "(Run/Rollback/Sync catalog), vui lòng thử lại sau ít giây"
        )

    monkeypatch.setattr(task_service.git_service, "check_commit_on_staging", raise_lock_timeout)

    result = task_service.create_task(
        db_session, user, group.id, "api", "abc1234", None, "release", datetime.utcnow() + timedelta(hours=1)
    )
    assert result.ok is False
    assert "đang bận" in result.message
    assert "Kiểm tra commit thất bại" in result.message

    # Khong duoc tao task rac khi check_commit_on_staging loi (loi xay ra truoc db.add).
    assert db_session.query(DeployTask).count() == task_count_before
