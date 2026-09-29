"""Kiểm thử vòng đời DeployTask (Task -> Approved -> Running -> Done/Rejected/Cancelled)
trong app/services/task_service.py.

Trọng tâm: state machine transition guard - action chỉ nên chặn theo trạng thái
NGUỒN hợp lệ, không chỉ chặn mỗi khi task đã Done.
"""

from datetime import datetime, timedelta

import pytest

from app.models import DeployTask, Group, Project, Role, TaskStatus
from app.services import task_service
from tests.conftest import make_user


def make_group(db_session, name="core"):
    group = Group(group_name=name, is_active=True)
    db_session.add(group)
    db_session.commit()
    db_session.refresh(group)
    return group


def make_project(db_session, group, application="api", is_active=True):
    """Tạo 1 dòng catalog Project (group_id/application_name) - sau fix B5,
    create_task bắt buộc application phải khớp CHÍNH XÁC 1 Project active thuộc
    group tương ứng."""
    project = Project(group_id=group.id, application_name=application, is_active=is_active)
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)
    return project


def make_task(db_session, status=TaskStatus.TASK, **kwargs):
    defaults = dict(
        project="core",
        application="api",
        commitid="abc1234",
        status=status,
        email="creator@example.com",
    )
    defaults.update(kwargs)
    task = DeployTask(**defaults)
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


# ---------------------------------------------------------------------------
# confirm_task
# ---------------------------------------------------------------------------


def test_confirm_task_happy_path_from_task_status(db_session):
    user = make_user(db_session, role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.TASK)

    result = task_service.approve_task(db_session, task.id, user)

    assert result.ok is True
    assert result.task.status == TaskStatus.APPROVED


def test_confirm_task_on_nonexistent_task_returns_error(db_session):
    user = make_user(db_session, role=Role.ADMIN)

    result = task_service.approve_task(db_session, 99999, user)

    assert result.ok is False
    assert "không tồn tại" in result.message


def test_confirm_task_blocks_when_done(db_session):
    user = make_user(db_session, role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.DONE)

    result = task_service.approve_task(db_session, task.id, user)

    assert result.ok is False


def test_confirm_task_should_block_when_already_rejected(db_session):
    user = make_user(db_session, role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.REJECTED)

    result = task_service.approve_task(db_session, task.id, user)

    assert result.ok is False, "Không được phép Approve 1 task đã Rejected"


def test_confirm_task_should_block_when_already_cancelled(db_session):
    user = make_user(db_session, role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.CANCELLED)

    result = task_service.approve_task(db_session, task.id, user)

    assert result.ok is False, "Không được phép Approve 1 task đã Cancelled"


def test_confirm_task_should_block_when_running(db_session):
    user = make_user(db_session, role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.RUNNING)

    result = task_service.approve_task(db_session, task.id, user)

    assert result.ok is False, "Không được phép Approve 1 task đang Running"


# ---------------------------------------------------------------------------
# reject_task
# ---------------------------------------------------------------------------


def test_reject_task_happy_path(db_session):
    user = make_user(db_session, role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.TASK)

    result = task_service.reject_task(db_session, task.id, user)

    assert result.ok is True
    assert result.task.status == TaskStatus.REJECTED


def test_reject_task_should_block_when_running(db_session):
    user = make_user(db_session, role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.RUNNING)

    result = task_service.reject_task(db_session, task.id, user)

    assert result.ok is False, "Không được phép Reject 1 task đang Running (đã Run thật)"


# ---------------------------------------------------------------------------
# run_task - quan trọng nhất vì hành động này push git thật lên master
# ---------------------------------------------------------------------------


def test_run_task_happy_path_from_confirmed(db_session, monkeypatch):
    """THAY ĐỔI HÀNH VI CÓ CHỦ Ý (hàng đợi chạy nền): run_task giờ CHỈ CAS task sang
    QUEUED và trả lời ngay - KHÔNG còn gọi git đồng bộ (xem TaskStatus.QUEUED,
    task_service.run_queued_task). perform_run bị monkeypatch ở đây để khẳng định RÕ RÀNG
    nó KHÔNG được gọi ở tầng run_task (khác hành vi cũ)."""
    user = make_user(db_session, role=Role.SUPER_ADMIN)
    task = make_task(db_session, status=TaskStatus.APPROVED)

    calls = []
    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: (calls.append(1) or task_service.git_service.RunResult(ok=True, message="ok", image_version="v1.2.3")),
    )

    result = task_service.run_task(db_session, task.id, user)

    assert result.ok is True
    assert result.task.status == TaskStatus.QUEUED
    assert result.task.queued_from_status == TaskStatus.APPROVED
    assert result.task.image_version is None, "image_version chỉ được set bởi worker sau khi push thật"
    assert calls == [], "run_task KHÔNG được gọi perform_run đồng bộ nữa (đã chuyển sang worker)"


def test_run_task_happy_path_from_task_status_without_confirm(db_session, monkeypatch):
    """THAY ĐỔI HÀNH VI CÓ CHỦ Ý: Run KHÔNG còn bắt buộc phải Approve trước - task mới
    tạo (status=TASK) phải vào hàng đợi (QUEUED) được trực tiếp, giống hệt APPROVED. Side
    effect chấp nhận được: maintainer_confirmed vẫn None vì bỏ qua bước Approve (không gây
    lỗi vì cột này nullable và không có nơi nào dereference thiếu kiểm tra None)."""
    user = make_user(db_session, role=Role.SUPER_ADMIN)
    task = make_task(db_session, status=TaskStatus.TASK)
    assert task.maintainer_confirmed is None

    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: task_service.git_service.RunResult(ok=True, message="ok", image_version="v1"),
    )

    result = task_service.run_task(db_session, task.id, user)

    assert result.ok is True, "Run phải thành công trực tiếp từ TASK, không cần Approve trước"
    assert result.task.status == TaskStatus.QUEUED
    assert result.task.queued_from_status == TaskStatus.TASK
    assert result.task.run_at is None, "run_at chỉ được set bởi worker khi thực sự chạy git"
    assert result.task.maintainer_run == user.email
    # side effect chấp nhận được: Approve bị bỏ qua nên field này vẫn None
    assert result.task.maintainer_confirmed is None


def test_run_task_message_no_longer_mentions_confirm_first(db_session, monkeypatch):
    """Message từ chối Run (cho trạng thái terminal) không còn nhắc 'phải Approve
    trước' vì Approve giờ không còn là điều kiện bắt buộc cho Run."""
    user = make_user(db_session, role=Role.SUPER_ADMIN)
    task = make_task(db_session, status=TaskStatus.DONE)

    result = task_service.run_task(db_session, task.id, user)

    assert result.ok is False
    assert "phải Approve trước" not in result.message
    assert "Task đang ở trạng thái" in result.message


def test_run_task_should_block_when_done(db_session, monkeypatch):
    """DONE là trạng thái terminal - Run lại 1 task đã Done phải bị chặn giống hệt
    hành vi cũ, không bị ảnh hưởng bởi việc mở rộng điều kiện cho phép Run từ TASK."""
    user = make_user(db_session, role=Role.SUPER_ADMIN)
    task = make_task(db_session, status=TaskStatus.DONE, image_version="v1")

    calls = []
    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: (calls.append(1) or task_service.git_service.RunResult(ok=True, message="ok", image_version="v2")),
    )

    result = task_service.run_task(db_session, task.id, user)

    assert result.ok is False, "Không được phép Run 1 task đã Done"
    assert len(calls) == 0


def test_run_task_should_block_when_already_running(db_session, monkeypatch):
    user = make_user(db_session, role=Role.SUPER_ADMIN)
    task = make_task(db_session, status=TaskStatus.RUNNING, image_version="v1")

    calls = []
    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: (calls.append(1) or task_service.git_service.RunResult(ok=True, message="ok", image_version="v2")),
    )

    result = task_service.run_task(db_session, task.id, user)

    assert result.ok is False, "Không được phép Run 1 task đang Running (double-run)"
    assert len(calls) == 0


def test_run_task_should_block_when_rejected(db_session, monkeypatch):
    user = make_user(db_session, role=Role.SUPER_ADMIN)
    task = make_task(db_session, status=TaskStatus.REJECTED)

    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: task_service.git_service.RunResult(ok=True, message="ok", image_version="v1"),
    )

    result = task_service.run_task(db_session, task.id, user)

    assert result.ok is False, "Không được phép Run 1 task đã Rejected"


def test_run_task_should_block_when_cancelled(db_session, monkeypatch):
    user = make_user(db_session, role=Role.SUPER_ADMIN)
    task = make_task(db_session, status=TaskStatus.CANCELLED)

    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: task_service.git_service.RunResult(ok=True, message="ok", image_version="v1"),
    )

    result = task_service.run_task(db_session, task.id, user)

    assert result.ok is False, "Không được phép Run 1 task đã Cancelled"


def test_run_task_git_failure_keeps_task_status_unchanged(db_session, monkeypatch):
    """DỜI SANG TẦNG WORKER: từ khi có hàng đợi, run_task() không còn gọi git nên không
    còn có thể "thất bại vì git" ở tầng đó nữa - lỗi git giờ chỉ có thể xảy ra ở
    run_queued_task() (worker). Khi git_service.perform_run thất bại (network lỗi/commit
    không còn hợp lệ), task phải được revert về đúng status TRƯỚC KHI vào hàng đợi
    (queued_from_status), KHÔNG được kẹt lại ở RUNNING - đây là hành vi tương đương bản cũ
    "không đổi trạng thái khi git lỗi", chỉ khác điểm quan sát."""
    user = make_user(db_session, role=Role.SUPER_ADMIN)
    task = make_task(db_session, status=TaskStatus.APPROVED)

    run_result = task_service.run_task(db_session, task.id, user)
    assert run_result.ok is True
    assert task.status == TaskStatus.QUEUED

    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: task_service.git_service.RunResult(ok=False, message="git push failed: network error"),
    )

    worker_result = task_service.run_queued_task(db_session, task.id)

    assert worker_result.ok is False
    assert "git push failed" in worker_result.message
    db_session.refresh(task)
    assert task.status == TaskStatus.APPROVED, "Task phải revert đúng về status trước khi vào hàng đợi, không kẹt ở RUNNING"
    assert task.queued_from_status is None


# ---------------------------------------------------------------------------
# cancel_task
# ---------------------------------------------------------------------------


def test_cancel_task_happy_path(db_session):
    user = make_user(db_session, role=Role.USER)
    task = make_task(db_session, status=TaskStatus.TASK, email=user.email)

    result = task_service.cancel_task(db_session, task.id, user)

    assert result.ok is True
    assert result.task.status == TaskStatus.CANCELLED


def test_cancel_task_blocks_when_running(db_session):
    user = make_user(db_session, role=Role.USER)
    task = make_task(db_session, status=TaskStatus.RUNNING, email=user.email)

    result = task_service.cancel_task(db_session, task.id, user)

    assert result.ok is False


def test_cancel_task_blocks_when_done(db_session):
    user = make_user(db_session, role=Role.USER)
    task = make_task(db_session, status=TaskStatus.DONE, email=user.email)

    result = task_service.cancel_task(db_session, task.id, user)

    assert result.ok is False


# ---------------------------------------------------------------------------
# create_task
# ---------------------------------------------------------------------------


def test_create_task_rejects_empty_updatefor(db_session, monkeypatch):
    user = make_user(db_session)
    group = make_group(db_session)

    result = task_service.create_task(
        db_session, user, group.id, "api", "abc1234", None, "   ", datetime.utcnow() + timedelta(hours=1)
    )

    assert result.ok is False
    assert "Mục đích" in result.message


def test_create_task_rejects_missing_group(db_session):
    user = make_user(db_session)

    result = task_service.create_task(
        db_session, user, 99999, "api", "abc1234", None, "release", datetime.utcnow() + timedelta(hours=1)
    )

    assert result.ok is False
    assert "Group không tồn tại" in result.message


def test_create_task_rejects_duplicate_open_task(db_session, monkeypatch):
    user = make_user(db_session)
    group = make_group(db_session)
    make_task(db_session, status=TaskStatus.TASK, project=group.group_name, application="api", commitid="abc1234")

    monkeypatch.setattr(
        task_service.git_service,
        "check_commit_on_staging",
        lambda *a, **k: task_service.git_service.CommitCheckResult(status="ok", message="ok", image_version="v1"),
    )

    result = task_service.create_task(
        db_session, user, group.id, "api", "abc1234", None, "release", datetime.utcnow() + timedelta(hours=1)
    )

    assert result.ok is False
    assert "task khác" in result.message or "đã có request" in result.message


def test_create_task_allows_new_request_after_previous_rejected(db_session, monkeypatch):
    """Task cũ Rejected/Cancelled không tính là 'đang mở' - request mới cho cùng commit
    phải được tạo bình thường."""
    user = make_user(db_session)
    group = make_group(db_session)
    make_project(db_session, group, application="api")
    make_task(db_session, status=TaskStatus.REJECTED, project=group.group_name, application="api", commitid="abc1234")

    monkeypatch.setattr(
        task_service.git_service,
        "check_commit_on_staging",
        lambda *a, **k: task_service.git_service.CommitCheckResult(status="ok", message="ok", image_version="v1"),
    )

    result = task_service.create_task(
        db_session, user, group.id, "api", "abc1234", None, "release", datetime.utcnow() + timedelta(hours=1)
    )

    assert result.ok is True


def test_create_task_rejects_application_not_in_catalog(db_session, monkeypatch):
    """FIXED (B5): application phải khớp CHÍNH XÁC 1 Project active thuộc group - nếu
    gửi thẳng 1 application tuỳ ý (không qua dropdown, không có trong catalog) request
    phải bị từ chối, kể cả khi commit check trên staging trả về ok."""
    user = make_user(db_session)
    group = make_group(db_session)
    # Không tạo Project catalog nào cho group này.

    monkeypatch.setattr(
        task_service.git_service,
        "check_commit_on_staging",
        lambda *a, **k: task_service.git_service.CommitCheckResult(status="ok", message="ok", image_version="v1"),
    )

    result = task_service.create_task(
        db_session, user, group.id, "not-in-catalog", "abc1234", None, "release", datetime.utcnow() + timedelta(hours=1)
    )

    assert result.ok is False
    assert "catalog" in result.message


def test_create_task_rejects_application_in_inactive_catalog_entry(db_session, monkeypatch):
    """Project catalog tồn tại nhưng is_active=False (đã bị vô hiệu hoá) vẫn phải bị
    coi như KHÔNG có trong catalog."""
    user = make_user(db_session)
    group = make_group(db_session)
    make_project(db_session, group, application="api", is_active=False)

    monkeypatch.setattr(
        task_service.git_service,
        "check_commit_on_staging",
        lambda *a, **k: task_service.git_service.CommitCheckResult(status="ok", message="ok", image_version="v1"),
    )

    result = task_service.create_task(
        db_session, user, group.id, "api", "abc1234", None, "release", datetime.utcnow() + timedelta(hours=1)
    )

    assert result.ok is False
    assert "catalog" in result.message


def test_create_task_commit_not_found_on_staging(db_session, monkeypatch):
    user = make_user(db_session)
    group = make_group(db_session)
    make_project(db_session, group, application="api")

    monkeypatch.setattr(
        task_service.git_service,
        "check_commit_on_staging",
        lambda *a, **k: task_service.git_service.CommitCheckResult(status="not_found", message="Không tìm thấy commit"),
    )

    result = task_service.create_task(
        db_session, user, group.id, "api", "abc1234", None, "release", datetime.utcnow() + timedelta(hours=1)
    )

    assert result.ok is False


# ---------------------------------------------------------------------------
# set_auto_deploy
# ---------------------------------------------------------------------------


def test_set_auto_deploy_requires_run_at_when_enabling(db_session):
    user = make_user(db_session, role=Role.SUPER_ADMIN)
    task = make_task(db_session, status=TaskStatus.APPROVED)

    result = task_service.set_auto_deploy(db_session, task.id, user, True, None)

    assert result.ok is False


def test_set_auto_deploy_blocks_on_terminal_status(db_session):
    user = make_user(db_session, role=Role.SUPER_ADMIN)
    task = make_task(db_session, status=TaskStatus.DONE)

    result = task_service.set_auto_deploy(db_session, task.id, user, True, datetime.utcnow() + timedelta(hours=1))

    assert result.ok is False


def test_set_auto_deploy_happy_path(db_session):
    user = make_user(db_session, role=Role.SUPER_ADMIN)
    task = make_task(db_session, status=TaskStatus.APPROVED)
    run_at = datetime.utcnow() + timedelta(hours=1)

    result = task_service.set_auto_deploy(db_session, task.id, user, True, run_at)

    assert result.ok is True
    assert task.auto_deploy is True
    assert task.auto_run_at == run_at


# ---------------------------------------------------------------------------
# rollback_task
# ---------------------------------------------------------------------------


def test_rollback_task_requires_done_status(db_session):
    user = make_user(db_session, role=Role.SUPER_ADMIN)
    task = make_task(db_session, status=TaskStatus.RUNNING)

    result = task_service.rollback_task(db_session, task.id, 1, user)

    assert result.ok is False


def test_rollback_task_blocks_when_window_closed(db_session):
    user = make_user(db_session, role=Role.SUPER_ADMIN)
    task = make_task(
        db_session,
        status=TaskStatus.DONE,
        image_version="v2",
        done_at=datetime.utcnow() - timedelta(days=10),
    )
    target = make_task(db_session, status=TaskStatus.DONE, image_version="v1", commitid="def5678")

    result = task_service.rollback_task(db_session, task.id, target.id, user)

    assert result.ok is False
    assert "quá" in result.message


def test_rollback_task_rejects_target_from_other_application(db_session):
    user = make_user(db_session, role=Role.SUPER_ADMIN)
    task = make_task(db_session, status=TaskStatus.DONE, image_version="v2", done_at=datetime.utcnow())
    other_app_target = make_task(
        db_session, status=TaskStatus.DONE, image_version="v1", application="other-app", commitid="def5678"
    )

    result = task_service.rollback_task(db_session, task.id, other_app_target.id, user)

    assert result.ok is False


def test_rollback_task_rejects_same_version_as_noop(db_session):
    user = make_user(db_session, role=Role.SUPER_ADMIN)
    task = make_task(db_session, status=TaskStatus.DONE, image_version="v1", done_at=datetime.utcnow())
    target = make_task(db_session, status=TaskStatus.DONE, image_version="v1", commitid="def5678")

    result = task_service.rollback_task(db_session, task.id, target.id, user)

    assert result.ok is False
    assert "hiện tại" in result.message


def test_rollback_task_happy_path_creates_new_queued_task(db_session, monkeypatch):
    """THAY ĐỔI HÀNH VI CÓ CHỦ Ý (hàng đợi): rollback_task giờ CHỈ INSERT 1 dòng mới
    status=QUEUED (kèm rollback_target_task_id) và trả lời ngay - KHÔNG còn gọi
    perform_rollback đồng bộ (xem TaskStatus.QUEUED, task_service.run_queued_task)."""
    user = make_user(db_session, role=Role.SUPER_ADMIN)
    task = make_task(db_session, status=TaskStatus.DONE, image_version="v2", done_at=datetime.utcnow())
    target = make_task(db_session, status=TaskStatus.DONE, image_version="v1", commitid="def5678")

    calls = []
    monkeypatch.setattr(
        task_service.git_service,
        "perform_rollback",
        lambda *a, **k: (calls.append(1) or task_service.git_service.RunResult(ok=True, message="ok", image_version="v1")),
    )

    result = task_service.rollback_task(db_session, task.id, target.id, user)

    assert result.ok is True
    assert result.task.id != task.id
    assert result.task.status == TaskStatus.QUEUED
    assert result.task.rollback_target_task_id == target.id
    # image_version da duoc COPY tu target ngay luc enqueue (khac Run thuong: khong
    # phai doi worker chay xong moi co) - dung de worker biet version can rollback ve.
    assert result.task.image_version == "v1"
    assert calls == [], "rollback_task KHÔNG được gọi perform_rollback đồng bộ nữa (đã chuyển sang worker)"
    # task cũ không bị sửa
    assert task.status == TaskStatus.DONE
