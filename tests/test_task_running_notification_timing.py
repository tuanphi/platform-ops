"""[TEST] Việc 1 + Việc 2 của bản vá "Telegram Task is Running".

Việc 1: trước đây `process_task_queue()` chạy XONG TOÀN BỘ hàng đợi rồi mới trả về,
scheduler mới gửi Telegram -> thông báo "Task is Running" đến khi task ĐÃ chạy xong (hoặc
đến hàng loạt cùng lúc sau task CUỐI CÙNG nếu xếp hàng nhiều task). Bản vá thêm callback
`on_running` gọi NGAY sau khi CAS QUEUED->RUNNING commit, TRƯỚC khi gọi git
(app/services/task_service.py::run_queued_task/process_task_queue,
app/scheduler.py::_notify_task_running/job_process_task_queue).

Việc 2: `_is_auto_deploy_task` (app/scheduler.py) dùng `maintainer_run ==
AUTO_DEPLOY_EMAIL` (KHÔNG dùng cột `auto_deploy`, cột này bị job_auto_deploy tự set False
ngay lúc enqueue) để quyết định có thêm hậu tố "(Auto Deploy)" vào Telegram hay không.

Test gọi thẳng `scheduler_module.job_process_task_queue()`/`job_auto_deploy()` (không chờ
APScheduler chạy thật), patch `SessionLocal` trỏ về `db_session` in-memory của test (theo
đúng convention `tests/test_reminder_scheduler.py`), monkeypatch
`task_service.git_service.perform_run` để không chạm git thật (theo đúng convention
`tests/test_task_queue_worker.py`)."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from app import scheduler as scheduler_module
from app.models import DeployTask, Role, TaskStatus
from app.services import task_service
from tests.conftest import make_user


def make_task(db_session, status=TaskStatus.QUEUED, **kwargs):
    defaults = dict(
        project="core",
        application="api",
        commitid="abc1234",
        status=status,
        email="owner@example.com",
    )
    defaults.update(kwargs)
    task = DeployTask(**defaults)
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


def _patch_scheduler_session(monkeypatch, db_session):
    monkeypatch.setattr(scheduler_module, "SessionLocal", lambda: db_session)


def _patch_perform_run_ok(monkeypatch, events=None, event_name="perform_run"):
    def _fake(project, application, commitid, run_by_email, config, background=False):
        if events is not None:
            events.append(event_name)
        return task_service.git_service.RunResult(ok=True, message="ok", image_version="v1")

    monkeypatch.setattr(task_service.git_service, "perform_run", _fake)


# ---------------------------------------------------------------------------
# Nhiệm vụ 1.1 - notify "Task is Running" PHẢI gửi TRƯỚC khi perform_run được gọi
# ---------------------------------------------------------------------------


def test_notify_running_sent_before_perform_run_is_called(db_session, monkeypatch):
    _patch_scheduler_session(monkeypatch, db_session)
    make_task(db_session, queued_from_status=TaskStatus.APPROVED, commitid="n000001")

    events: list[str] = []
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: events.append("telegram"))
    _patch_perform_run_ok(monkeypatch, events)

    scheduler_module.job_process_task_queue()

    assert events == ["telegram", "perform_run"], (
        f"Telegram 'Task is Running' phai gui TRUOC khi perform_run duoc goi, thu tu thuc te: {events}"
    )


def test_notify_running_message_content_and_no_failure_alert_on_success(db_session, monkeypatch):
    _patch_scheduler_session(monkeypatch, db_session)
    make_task(db_session, queued_from_status=TaskStatus.APPROVED, commitid="n000002")

    sent: list[str] = []
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: sent.append(msg))
    _patch_perform_run_ok(monkeypatch)

    scheduler_module.job_process_task_queue()

    assert len(sent) == 1, "Task thanh cong: chi 1 thong bao 'Task is Running', khong co canh bao that bai"
    assert "Task is Running" in sent[0]


# ---------------------------------------------------------------------------
# Nhiệm vụ 1.2 - xếp hàng 3 task: notify(task N) phải gửi TRƯỚC khi task N+1 bắt đầu chạy
# (không được dồn tất cả notify tới cuối vòng lặp như hành vi cũ)
# ---------------------------------------------------------------------------


def test_three_queued_tasks_notify_interleaved_with_each_run_not_batched_at_end(db_session, monkeypatch):
    _patch_scheduler_session(monkeypatch, db_session)
    for i in range(3):
        make_task(
            db_session,
            queued_from_status=TaskStatus.APPROVED,
            commitid=f"n00001{i}",
            application=f"app{i}",
        )

    events: list[str] = []
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: events.append("telegram"))
    _patch_perform_run_ok(monkeypatch, events)

    scheduler_module.job_process_task_queue()

    assert events == ["telegram", "perform_run"] * 3, (
        "Moi task phai duoc bao 'Running' NGAY TRUOC khi no chay, khong duoc doi ca vong "
        f"lap xong roi gui hang loat (hanh vi cu) - thu tu thuc te: {events}"
    )


# ---------------------------------------------------------------------------
# Nhiệm vụ 1.3 - send_telegram raise exception -> task vẫn chạy bình thường, không kẹt
# ---------------------------------------------------------------------------


def test_send_telegram_exception_does_not_block_task_from_running(db_session, monkeypatch):
    _patch_scheduler_session(monkeypatch, db_session)
    task = make_task(db_session, queued_from_status=TaskStatus.APPROVED, commitid="n000020")

    def _boom(msg):
        raise ConnectionError("telegram network unreachable")

    monkeypatch.setattr(scheduler_module, "send_telegram", _boom)
    calls: list[str] = []
    _patch_perform_run_ok(monkeypatch, calls)

    scheduler_module.job_process_task_queue()  # KHONG duoc raise ra ngoai

    assert calls == ["perform_run"], "perform_run van phai duoc goi du callback notify loi"
    reloaded = db_session.get(DeployTask, task.id)
    assert reloaded.status == TaskStatus.RUNNING, "Task khong duoc ket lai o QUEUED/trang thai trung gian do loi Telegram"


def test_send_telegram_exception_on_first_of_two_tasks_does_not_block_second_task(db_session, monkeypatch):
    """Loi Telegram cho 1 task khong duoc lam hong ca luot xu ly hang doi (task ke tiep
    van phai duoc CAS sang RUNNING va chay git binh thuong)."""
    _patch_scheduler_session(monkeypatch, db_session)
    task1 = make_task(db_session, queued_from_status=TaskStatus.APPROVED, commitid="n000021")
    task2 = make_task(
        db_session, queued_from_status=TaskStatus.APPROVED, commitid="n000022", application="worker"
    )
    task1_id, task2_id = task1.id, task2.id

    def _flaky_telegram(msg):
        raise TimeoutError("telegram timeout")

    monkeypatch.setattr(scheduler_module, "send_telegram", _flaky_telegram)
    calls: list[str] = []
    _patch_perform_run_ok(monkeypatch, calls)

    scheduler_module.job_process_task_queue()

    assert calls == ["perform_run", "perform_run"]
    for task_id in (task1_id, task2_id):
        reloaded = db_session.get(DeployTask, task_id)
        assert reloaded.status == TaskStatus.RUNNING


# ---------------------------------------------------------------------------
# Nhiệm vụ 1.4 - task thất bại (perform_run trả ok=False) -> vẫn gửi cảnh báo thất bại
# ---------------------------------------------------------------------------


def test_task_failure_still_sends_failure_alert(db_session, monkeypatch):
    _patch_scheduler_session(monkeypatch, db_session)
    task = make_task(db_session, queued_from_status=TaskStatus.TASK, commitid="n000030")
    task_id = task.id

    sent: list[str] = []
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: sent.append(msg))
    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: task_service.git_service.RunResult(ok=False, message="git push failed: staging lock timeout"),
    )

    scheduler_module.job_process_task_queue()

    assert len(sent) == 2, f"Phai co 2 thong bao: 'Running' roi 'that bai', thuc te: {sent}"
    assert "Task is Running" in sent[0]
    assert "Task execution failed" in sent[1]
    assert "staging lock timeout" in sent[1]
    reloaded = db_session.get(DeployTask, task_id)
    assert reloaded.status == TaskStatus.TASK, "Phai revert dung ve status truoc do, khong ket o RUNNING"


# ---------------------------------------------------------------------------
# Nhiệm vụ 2.1 - Task do NGƯỜI DÙNG bấm Run -> KHÔNG có hậu tố "(Auto Deploy)"
# ---------------------------------------------------------------------------


def test_manual_run_notification_has_no_auto_deploy_suffix(db_session, monkeypatch):
    _patch_scheduler_session(monkeypatch, db_session)
    admin = make_user(db_session, email="manual-runner@example.com", role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.APPROVED, commitid="n000040")

    # Bam Run that (qua task_service.run_task, giong nguoi dung bam nut Run tren UI).
    result = task_service.run_task(db_session, task.id, admin)
    assert result.ok is True

    sent: list[str] = []
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: sent.append(msg))
    _patch_perform_run_ok(monkeypatch)

    scheduler_module.job_process_task_queue()

    assert len(sent) == 1
    assert "Task is Running" in sent[0]
    assert "Auto Deploy" not in sent[0], f"Task nguoi dung bam Run KHONG duoc gan nham '(Auto Deploy)': {sent[0]}"
    assert "manual-runner@example.com" in sent[0]


# ---------------------------------------------------------------------------
# Nhiệm vụ 2.2 - Task do job_auto_deploy đưa vào hàng đợi -> CÓ hậu tố "(Auto Deploy)"
# ---------------------------------------------------------------------------


def test_auto_deploy_run_notification_has_auto_deploy_suffix(db_session, monkeypatch):
    _patch_scheduler_session(monkeypatch, db_session)
    task = make_task(
        db_session,
        status=TaskStatus.APPROVED,
        commitid="n000041",
        auto_deploy=True,
        auto_run_at=datetime.utcnow() - timedelta(minutes=1),
    )
    task_id = task.id

    fail_alerts: list[str] = []
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: fail_alerts.append(msg))
    scheduler_module.job_auto_deploy()

    reloaded = db_session.get(DeployTask, task_id)
    assert reloaded.status == TaskStatus.QUEUED, "job_auto_deploy phai dua task vao hang doi"
    assert fail_alerts == [], "job_auto_deploy khong duoc gui canh bao khi enqueue thanh cong"

    sent: list[str] = []
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: sent.append(msg))
    _patch_perform_run_ok(monkeypatch)

    scheduler_module.job_process_task_queue()

    assert len(sent) == 1
    assert "Task is Running (Auto Deploy)" in sent[0], f"Thieu hau to '(Auto Deploy)': {sent[0]}"


# ---------------------------------------------------------------------------
# Nhiệm vụ 2.3 - Tự kiểm chứng khẳng định của dev: job_auto_deploy CÓ thật sự set
# auto_deploy=False NGAY LÚC enqueue (trước khi worker kịp chạy) hay không?
# ---------------------------------------------------------------------------


def test_job_auto_deploy_sets_auto_deploy_false_immediately_at_enqueue_time(db_session, monkeypatch):
    """Xac minh khang dinh cua dev (scheduler.py:238-242, docstring job_auto_deploy):
    cot auto_deploy da la False trong DB NGAY SAU khi job_auto_deploy() chay xong, TRUOC
    khi process_task_queue/worker kip chay - vi vay maintainer_run moi la dau hieu dung
    de phan biet nguon goc Auto Deploy, KHONG the dung lai cot auto_deploy."""
    _patch_scheduler_session(monkeypatch, db_session)
    task = make_task(
        db_session,
        status=TaskStatus.APPROVED,
        commitid="n000042",
        auto_deploy=True,
        auto_run_at=datetime.utcnow() - timedelta(minutes=1),
    )
    task_id = task.id
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: None)

    scheduler_module.job_auto_deploy()

    # Doc lai TRUC TIEP tu DB (khong qua Python object cache) truoc khi worker chay -
    # xac nhan auto_deploy DA False that su trong DB, dung nhu dev mo ta.
    row = db_session.get(DeployTask, task_id)
    assert row.auto_deploy is False, "Dev noi dung: auto_deploy bi set False ngay luc enqueue, khong con dung duoc"
    assert row.status == TaskStatus.QUEUED
    assert row.maintainer_run == scheduler_module.AUTO_DEPLOY_EMAIL


# ---------------------------------------------------------------------------
# Nhiệm vụ 2.4 - Task CÓ auto_deploy=True (cột cũ) nhưng được NGƯỜI DÙNG bấm Run tay
# (trước giờ hẹn) -> KHÔNG được gắn nhầm "(Auto Deploy)" (chứng minh vì sao dùng
# maintainer_run đúng đắn hơn cột auto_deploy đã lỗi thời).
# ---------------------------------------------------------------------------


def test_manual_run_of_task_with_stale_auto_deploy_flag_is_not_mislabeled(db_session, monkeypatch):
    _patch_scheduler_session(monkeypatch, db_session)
    admin = make_user(db_session, email="human@example.com", role=Role.ADMIN)
    # auto_deploy=True nhung chua toi gio hen - nguoi dung tu bam Run truoc.
    task = make_task(
        db_session,
        status=TaskStatus.APPROVED,
        commitid="n000043",
        auto_deploy=True,
        auto_run_at=datetime.utcnow() + timedelta(hours=1),
    )

    result = task_service.run_task(db_session, task.id, admin)
    assert result.ok is True

    sent: list[str] = []
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: sent.append(msg))
    _patch_perform_run_ok(monkeypatch)

    scheduler_module.job_process_task_queue()

    assert len(sent) == 1
    assert "Auto Deploy" not in sent[0], (
        f"Cot auto_deploy=True con sot lai KHONG duoc dung de gan nhan - phai dua vao "
        f"maintainer_run: {sent[0]}"
    )


# ---------------------------------------------------------------------------
# Nhiệm vụ 2.4 (tiếp) - rollback_task cũng ghi maintainer_run = email người bấm Rollback,
# KHÔNG bao giờ vô tình trùng AUTO_DEPLOY_EMAIL trong luồng bình thường.
# ---------------------------------------------------------------------------


def test_rollback_task_sets_maintainer_run_to_real_user_not_auto_deploy_label(db_session, monkeypatch):
    admin = make_user(db_session, email="rollback-user@example.com", role=Role.SUPER_ADMIN)
    current = make_task(
        db_session, status=TaskStatus.DONE, image_version="v2", commitid="n000050", done_at=datetime.utcnow()
    )
    target = make_task(db_session, status=TaskStatus.DONE, image_version="v1", commitid="n000051")

    result = task_service.rollback_task(db_session, current.id, target.id, admin)
    assert result.ok is True
    assert result.task.maintainer_run == "rollback-user@example.com"
    assert result.task.maintainer_run != scheduler_module.AUTO_DEPLOY_EMAIL
