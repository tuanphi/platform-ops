"""Test luồng chính (main flow) của tính năng HÀNG ĐỢI CHẠY NỀN cho Run/Rollback - giải
pháp cho bug production: bấm Run 3 task gần cùng lúc -> 1 task chạy, 2 task lỗi vì không
giành được git lock (chỉ chờ 2 giây rồi raise GitError, xem
`app/services/git_service.py::_GIT_LOCK_TIMEOUT_SECONDS`).

Kể từ bản vá này: bấm Run/Rollback CHỈ CAS/INSERT task sang TaskStatus.QUEUED và trả lời
NGAY - không còn gọi git trên đường request. 1 worker nền (task_service.process_task_queue,
gọi định kỳ từ app/scheduler.py::job_process_task_queue mỗi 5s) lấy từng task QUEUED ra
chạy TUẦN TỰ (FIFO theo id, tôn trọng AppSetting.max_concurrent_running_tasks).

BẮT BUỘC monkeypatch git_service.perform_run/perform_rollback ở mọi test - không chạm
git/mạng thật."""

from __future__ import annotations

from datetime import datetime

from app.models import DeployTask, Role, TaskStatus
from app.services import task_service
from app.services.app_setting_service import update_app_setting
from tests.conftest import make_user


def make_task(db_session, status=TaskStatus.TASK, **kwargs):
    defaults = dict(project="core", application="api", commitid="abc1234", status=status, email="owner@example.com")
    defaults.update(kwargs)
    task = DeployTask(**defaults)
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


# ---------------------------------------------------------------------------
# 1. Bấm Run -> QUEUED ngay, perform_run KHÔNG được gọi tại thời điểm bấm
# ---------------------------------------------------------------------------


def test_run_enqueues_immediately_without_calling_git(client_factory, db_session, monkeypatch):
    admin = make_user(db_session, email="q-run@example.com", role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.APPROVED, commitid="q000001")

    calls = []
    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: (calls.append(1) or task_service.git_service.RunResult(ok=True, message="ok", image_version="v1")),
    )

    client = client_factory(user=admin)
    resp = client.post(f"/tasks/{task.id}/run", data={}, headers={"Accept": "application/json"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED
    assert task.queued_from_status == TaskStatus.APPROVED
    assert calls == [], "perform_run KHÔNG được gọi ngay lúc bấm Run - chỉ worker mới gọi"


# ---------------------------------------------------------------------------
# 2. Chạy process_task_queue -> task chuyển RUNNING rồi hoàn tất, perform_run gọi đúng 1 lần
# ---------------------------------------------------------------------------


def test_process_task_queue_promotes_queued_task_to_running_and_calls_git_once(db_session, monkeypatch):
    task = make_task(db_session, status=TaskStatus.QUEUED, queued_from_status=TaskStatus.APPROVED, commitid="q000002")

    calls = []
    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: (calls.append(1) or task_service.git_service.RunResult(ok=True, message="ok", image_version="v1.0.0")),
    )

    results = task_service.process_task_queue(db_session)

    assert len(results) == 1
    result_task, result = results[0]
    assert result.ok is True
    assert result_task.id == task.id
    assert calls == [1]
    db_session.refresh(task)
    assert task.status == TaskStatus.RUNNING
    assert task.image_version == "v1.0.0"
    assert task.run_at is not None
    assert task.queued_from_status is None


# ---------------------------------------------------------------------------
# 3. Kịch bản gốc của bug: enqueue 3 task rồi chạy worker -> cả 3 đều thành công
# ---------------------------------------------------------------------------


def test_three_tasks_enqueued_together_all_succeed_via_worker_no_git_lock_errors(
    client_factory, db_session, monkeypatch
):
    """Đây chính là kịch bản người dùng gặp trong production: bấm Run 3 task GẦN NHAU ->
    trước đây 2/3 task lỗi vì không giành được git lock trong 2s. Nay cả 3 phải thành
    công vì chỉ 1 worker DUY NHẤT xử lý tuần tự, không ai tranh chấp lock trên đường
    request nữa."""
    admin = make_user(db_session, email="q-bug@example.com", role=Role.ADMIN)
    task1 = make_task(db_session, status=TaskStatus.APPROVED, commitid="q000003")
    task2 = make_task(db_session, status=TaskStatus.APPROVED, commitid="q000004", application="worker")
    task3 = make_task(db_session, status=TaskStatus.APPROVED, commitid="q000005", application="scheduler")

    calls = []
    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: (calls.append(1) or task_service.git_service.RunResult(ok=True, message="ok", image_version="v1")),
    )

    client = client_factory(user=admin)
    for task in (task1, task2, task3):
        resp = client.post(f"/tasks/{task.id}/run", data={}, headers={"Accept": "application/json"})
        assert resp.status_code == 200 and resp.json()["ok"] is True

    for task in (task1, task2, task3):
        db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED

    results = task_service.process_task_queue(db_session)

    assert len(results) == 3
    assert all(result.ok for _, result in results), [r.message for _, r in results]
    assert len(calls) == 3
    for task in (task1, task2, task3):
        db_session.refresh(task)
        assert task.status == TaskStatus.RUNNING


# ---------------------------------------------------------------------------
# 4. FIFO: task enqueue trước được chạy trước
# ---------------------------------------------------------------------------


def test_worker_processes_queue_in_fifo_order_by_id(db_session, monkeypatch):
    update_app_setting(db_session, max_concurrent_running_tasks=1)

    older = make_task(db_session, status=TaskStatus.QUEUED, queued_from_status=TaskStatus.APPROVED, commitid="q000006")
    newer = make_task(
        db_session,
        status=TaskStatus.QUEUED,
        queued_from_status=TaskStatus.APPROVED,
        commitid="q000007",
        application="worker",
    )
    assert older.id < newer.id

    order = []
    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda project, application, commitid, *a, **k: (
            order.append(commitid) or task_service.git_service.RunResult(ok=True, message="ok", image_version="v1")
        ),
    )

    # limit=1 -> mỗi lượt gọi process_task_queue chỉ được lấy đúng 1 task; task cũ hơn
    # (id nhỏ hơn) phải được ưu tiên xử lý trước.
    first_pass = task_service.process_task_queue(db_session)
    assert len(first_pass) == 1
    assert first_pass[0][0].id == older.id
    assert order == ["q000006"]

    db_session.refresh(newer)
    assert newer.status == TaskStatus.QUEUED, "Task mới hơn vẫn phải chờ ở QUEUED do limit=1 và task cũ đang Running"


# ---------------------------------------------------------------------------
# 5. max_concurrent_running_tasks: =1 chỉ đưa 1 task sang RUNNING mỗi lượt; =0 không giới hạn
# ---------------------------------------------------------------------------


def test_max_concurrent_running_tasks_1_promotes_only_one_task_per_pass(db_session, monkeypatch):
    update_app_setting(db_session, max_concurrent_running_tasks=1)

    task1 = make_task(db_session, status=TaskStatus.QUEUED, queued_from_status=TaskStatus.APPROVED, commitid="q000008")
    task2 = make_task(
        db_session,
        status=TaskStatus.QUEUED,
        queued_from_status=TaskStatus.APPROVED,
        commitid="q000009",
        application="worker",
    )

    calls = []
    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: (calls.append(1) or task_service.git_service.RunResult(ok=True, message="ok", image_version="v1")),
    )

    results = task_service.process_task_queue(db_session)

    assert len(results) == 1
    assert len(calls) == 1
    db_session.refresh(task1)
    db_session.refresh(task2)
    assert task1.status == TaskStatus.RUNNING
    assert task2.status == TaskStatus.QUEUED, "Task thứ 2 phải chờ lượt sau vì limit=1 và task1 vừa chiếm slot"


def test_max_concurrent_running_tasks_0_means_unlimited(db_session, monkeypatch):
    update_app_setting(db_session, max_concurrent_running_tasks=0)

    tasks = [
        make_task(
            db_session,
            status=TaskStatus.QUEUED,
            queued_from_status=TaskStatus.APPROVED,
            commitid=f"q00001{i}",
            application=f"app{i}",
        )
        for i in range(3)
    ]

    calls = []
    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: (calls.append(1) or task_service.git_service.RunResult(ok=True, message="ok", image_version="v1")),
    )

    results = task_service.process_task_queue(db_session)

    assert len(results) == 3
    assert len(calls) == 3
    for task in tasks:
        db_session.refresh(task)
        assert task.status == TaskStatus.RUNNING


# ---------------------------------------------------------------------------
# 6. Cancel task đang QUEUED -> thành công, worker sau đó KHÔNG chạy task đã huỷ
# ---------------------------------------------------------------------------


def test_cancel_queued_task_succeeds_and_worker_skips_it_afterwards(db_session, monkeypatch):
    admin = make_user(db_session, email="q-cancel@example.com", role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.QUEUED, queued_from_status=TaskStatus.APPROVED, commitid="q000020")

    cancel_result = task_service.cancel_task(db_session, task.id, admin)
    assert cancel_result.ok is True
    db_session.refresh(task)
    assert task.status == TaskStatus.CANCELLED

    calls = []
    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: (calls.append(1) or task_service.git_service.RunResult(ok=True, message="ok", image_version="v1")),
    )

    results = task_service.process_task_queue(db_session)

    assert results == [], "Worker không được lấy task đã Cancelled ra xử lý"
    assert calls == []
    db_session.refresh(task)
    assert task.status == TaskStatus.CANCELLED


# ---------------------------------------------------------------------------
# 7. Task chạy lỗi ở worker -> revert đúng trạng thái cũ, không kẹt ở RUNNING
# ---------------------------------------------------------------------------


def test_worker_run_failure_reverts_to_queued_from_status_not_stuck_running(db_session, monkeypatch):
    task = make_task(db_session, status=TaskStatus.QUEUED, queued_from_status=TaskStatus.TASK, commitid="q000021")

    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: task_service.git_service.RunResult(ok=False, message="git push failed: staging lock timeout"),
    )

    results = task_service.process_task_queue(db_session)

    assert len(results) == 1
    result_task, result = results[0]
    assert result.ok is False
    assert "staging lock timeout" in result.message
    db_session.refresh(task)
    assert task.status == TaskStatus.TASK, "Phải revert đúng về status TRƯỚC khi vào hàng đợi, không kẹt ở RUNNING"
    assert task.queued_from_status is None


def test_worker_run_unexpected_exception_reverts_and_does_not_crash_the_job(db_session, monkeypatch):
    """process_task_queue KHÔNG được để 1 exception ngoài dự kiến từ perform_run làm chết
    cả job scheduler - phải bắt lại, revert task, rồi tiếp tục xử lý task khác."""
    failing_task = make_task(
        db_session, status=TaskStatus.QUEUED, queued_from_status=TaskStatus.APPROVED, commitid="q000022"
    )
    healthy_task = make_task(
        db_session,
        status=TaskStatus.QUEUED,
        queued_from_status=TaskStatus.APPROVED,
        commitid="q000023",
        application="worker",
    )

    def flaky_perform_run(project, application, *a, **k):
        if application == "api":
            raise ConnectionError("network unreachable")
        return task_service.git_service.RunResult(ok=True, message="ok", image_version="v1")

    monkeypatch.setattr(task_service.git_service, "perform_run", flaky_perform_run)

    results = task_service.process_task_queue(db_session)

    # failing_task raise exception ngoài dự kiến trong run_queued_task - process_task_queue
    # phải tự bắt (KHÔNG để lan ra job_process_task_queue), rollback rồi tiếp tục xử lý
    # healthy_task bình thường.
    db_session.refresh(failing_task)
    assert failing_task.status == TaskStatus.APPROVED, "Task lỗi ngoài dự kiến vẫn phải được revert, không kẹt RUNNING"
    db_session.refresh(healthy_task)
    assert healthy_task.status == TaskStatus.RUNNING, "Task lành phải vẫn được xử lý dù task khác trong cùng lượt lỗi"


# ---------------------------------------------------------------------------
# 8. Rollback qua hàng đợi: tạo task mới QUEUED, worker chạy đúng perform_rollback với target đúng
# ---------------------------------------------------------------------------


def test_rollback_via_queue_creates_queued_row_then_worker_calls_perform_rollback_with_correct_target(
    db_session, monkeypatch
):
    admin = make_user(db_session, email="q-rollback@example.com", role=Role.SUPER_ADMIN)
    current = make_task(
        db_session, status=TaskStatus.DONE, image_version="v2", commitid="q000024", done_at=datetime.utcnow()
    )
    target = make_task(db_session, status=TaskStatus.DONE, image_version="v1", commitid="q000025")

    enqueue_result = task_service.rollback_task(db_session, current.id, target.id, admin)
    assert enqueue_result.ok is True
    rollback_row = enqueue_result.task
    assert rollback_row.status == TaskStatus.QUEUED
    assert rollback_row.rollback_target_task_id == target.id
    assert rollback_row.image_version == "v1"

    captured_args = {}

    def fake_perform_rollback(project, application, image_version, run_by_email, config, background=False):
        captured_args.update(
            project=project,
            application=application,
            image_version=image_version,
            run_by_email=run_by_email,
            background=background,
        )
        return task_service.git_service.RunResult(ok=True, message="ok", image_version=image_version)

    monkeypatch.setattr(task_service.git_service, "perform_rollback", fake_perform_rollback)

    results = task_service.process_task_queue(db_session)

    assert len(results) == 1
    result_task, result = results[0]
    assert result.ok is True
    assert captured_args["project"] == current.project
    assert captured_args["application"] == current.application
    assert captured_args["image_version"] == "v1"
    assert captured_args["background"] is True

    db_session.refresh(rollback_row)
    assert rollback_row.status == TaskStatus.RUNNING
    assert rollback_row.image_version == "v1"
    # task nguồn (current) và target không bị đụng tới bởi worker.
    db_session.refresh(current)
    db_session.refresh(target)
    assert current.status == TaskStatus.DONE
    assert target.status == TaskStatus.DONE
