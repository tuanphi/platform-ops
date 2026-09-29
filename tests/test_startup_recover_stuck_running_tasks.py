"""Chốt chặn cho cơ chế phục hồi task kẹt ở RUNNING lúc app khởi động (xem
app/services/task_service.py::recover_stuck_running_tasks, gọi 1 lần từ
app/main.py::_recover_stuck_running_tasks_on_startup trước start_scheduler()).

Đây là logic TỰ ĐỘNG đổi trạng thái task lúc khởi động - sai là hỏng dữ liệu thật,
nên mọi nhánh (task Run bình thường / Rollback / task còn quá mới / QUEUED / trạng
thái cuối / queued_from_status NULL / nhiều task cùng lúc / lỗi Telegram) đều phải
có test khoá lại hành vi hiện tại, KHÔNG suy luận suông.

Không khởi động scheduler thật, không gọi Telegram/git thật - mọi I/O ngoài đều bị
monkeypatch."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pytest

from app.models import DeployTask, TaskStatus
from app.services import task_service


def make_running_task(db_session, *, age_seconds: float, **kwargs) -> DeployTask:
    defaults = dict(
        project="core",
        application="api",
        commitid="abc1234",
        email="owner@example.com",
        status=TaskStatus.RUNNING,
    )
    defaults.update(kwargs)
    task = DeployTask(**defaults)
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    # updated_at có default/onupdate ở tầng ORM (datetime.utcnow) - phải UPDATE thô qua
    # Core (không đi qua session đang giữ instance) để ép mốc thời gian "cũ" giả lập task
    # kẹt bấy lâu, tránh onupdate ghi đè lại giá trị mới ngay khi flush.
    from sqlalchemy import update as sa_update

    stale_at = datetime.utcnow() - timedelta(seconds=age_seconds)
    db_session.execute(sa_update(DeployTask).where(DeployTask.id == task.id).values(updated_at=stale_at))
    db_session.commit()
    db_session.refresh(task)
    assert task.updated_at == stale_at
    return task


# ---------------------------------------------------------------------------
# 1. Task Run bình thường, kẹt RUNNING > 120s -> về đúng queued_from_status
# ---------------------------------------------------------------------------


def test_old_running_normal_run_task_reverts_to_queued_from_status(db_session):
    task = make_running_task(db_session, age_seconds=200, queued_from_status=TaskStatus.APPROVED)

    recovered = task_service.recover_stuck_running_tasks(db_session)

    assert [t.id for t in recovered] == [task.id]
    db_session.refresh(task)
    assert task.status == TaskStatus.APPROVED
    assert task.queued_from_status is None


def test_old_running_normal_run_task_from_task_status_reverts_to_task(db_session):
    task = make_running_task(db_session, age_seconds=200, queued_from_status=TaskStatus.TASK)

    task_service.recover_stuck_running_tasks(db_session)

    db_session.refresh(task)
    assert task.status == TaskStatus.TASK
    assert task.queued_from_status is None


# ---------------------------------------------------------------------------
# 2. Task Rollback, kẹt RUNNING > 120s -> REJECTED, lý do nối vào updatefor
# ---------------------------------------------------------------------------


def test_old_running_rollback_task_becomes_rejected_with_reason_appended_to_updatefor(db_session):
    task = make_running_task(
        db_session,
        age_seconds=200,
        rollback_target_task_id=999,
        updatefor="rollback vi loi commit truoc",
        queued_from_status=None,
    )

    recovered = task_service.recover_stuck_running_tasks(db_session)

    assert [t.id for t in recovered] == [task.id]
    db_session.refresh(task)
    assert task.status == TaskStatus.REJECTED
    assert "rollback vi loi commit truoc" in task.updatefor
    assert "phục hồi" in task.updatefor.lower() or "Running" in task.updatefor


def test_old_running_rollback_task_with_empty_updatefor_does_not_crash(db_session):
    """updatefor rỗng/None ban đầu (không có lý do cũ) - ghép chuỗi vẫn phải an toàn,
    không crash, không để lại 'None' trong nội dung."""
    task = make_running_task(
        db_session, age_seconds=200, rollback_target_task_id=999, updatefor=None, queued_from_status=None
    )

    task_service.recover_stuck_running_tasks(db_session)

    db_session.refresh(task)
    assert task.status == TaskStatus.REJECTED
    assert task.updatefor is not None
    assert "None" not in task.updatefor


# ---------------------------------------------------------------------------
# 3. Task RUNNING mới (< 120s) -> KHÔNG bị đụng (chốt chặn chống cướp task đang chạy thật)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("age_seconds", [0, 1, 30, 60, 119, 119.9])
def test_recent_running_task_below_threshold_not_touched(db_session, age_seconds):
    task = make_running_task(db_session, age_seconds=age_seconds, queued_from_status=TaskStatus.APPROVED)

    recovered = task_service.recover_stuck_running_tasks(db_session)

    assert recovered == []
    db_session.refresh(task)
    assert task.status == TaskStatus.RUNNING
    assert task.queued_from_status == TaskStatus.APPROVED


def test_running_task_just_under_threshold_not_touched_strict_boundary(db_session):
    """Filter dùng `updated_at < threshold` (strict less-than, xem recover_stuck_running_tasks) -
    task còn CHƯA vượt ngưỡng dù chỉ 1 giây cũng không được coi là kẹt, tránh off-by-one
    recover quá sớm. age_seconds cố tình nhỏ hơn hẳn ngưỡng vài giây (thay vì đúng bằng
    ngưỡng) để không phụ thuộc vào thời điểm truy vấn thực thi (tránh test tự flaky)."""
    task = make_running_task(db_session, age_seconds=task_service._STARTUP_RECOVERY_MIN_STUCK_SECONDS - 5)

    recovered = task_service.recover_stuck_running_tasks(db_session)

    assert recovered == [], "Task chua vuot nguong 120s khong duoc coi la ket"


# ---------------------------------------------------------------------------
# 4. Task QUEUED -> không bị đụng (worker tự nhặt)
# ---------------------------------------------------------------------------


def test_queued_task_not_touched_even_if_old(db_session):
    task = DeployTask(
        project="core",
        application="api",
        commitid="abc1234",
        email="owner@example.com",
        status=TaskStatus.QUEUED,
        queued_from_status=TaskStatus.APPROVED,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    from sqlalchemy import update as sa_update

    db_session.execute(
        sa_update(DeployTask).where(DeployTask.id == task.id).values(updated_at=datetime.utcnow() - timedelta(seconds=999))
    )
    db_session.commit()

    recovered = task_service.recover_stuck_running_tasks(db_session)

    assert recovered == []
    db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED


# ---------------------------------------------------------------------------
# 5. Trạng thái cuối/chưa vào hàng đợi -> không bị đụng
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [TaskStatus.DONE, TaskStatus.REJECTED, TaskStatus.CANCELLED, TaskStatus.TASK, TaskStatus.APPROVED],
)
def test_terminal_and_not_yet_running_statuses_not_touched(db_session, status):
    task = DeployTask(
        project="core", application="api", commitid="abc1234", email="owner@example.com", status=status
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    from sqlalchemy import update as sa_update

    db_session.execute(
        sa_update(DeployTask).where(DeployTask.id == task.id).values(updated_at=datetime.utcnow() - timedelta(seconds=999))
    )
    db_session.commit()

    recovered = task_service.recover_stuck_running_tasks(db_session)

    assert recovered == []
    db_session.refresh(task)
    assert task.status == status


# ---------------------------------------------------------------------------
# 6. queued_from_status NULL trên task RUNNING kẹt (dữ liệu cũ/bất thường) -> không crash
# ---------------------------------------------------------------------------


def test_running_task_with_null_queued_from_status_does_not_crash_and_falls_back_to_task(db_session):
    task = make_running_task(db_session, age_seconds=200, queued_from_status=None, rollback_target_task_id=None)

    recovered = task_service.recover_stuck_running_tasks(db_session)

    assert [t.id for t in recovered] == [task.id]
    db_session.refresh(task)
    # Code hiện tại: fallback_status = task.queued_from_status or TaskStatus.TASK
    assert task.status == TaskStatus.TASK, (
        "queued_from_status NULL tren task Run kẹt -> code fallback ve TaskStatus.TASK. Neu day "
        "khong phai hanh vi mong muon (vd du lieu cu that su thuoc trang thai khac) can Leader "
        "xac nhan lai voi dev."
    )


# ---------------------------------------------------------------------------
# 7. Nhiều task treo cùng lúc -> phục hồi hết, 1 task lỗi không chặn task còn lại
# ---------------------------------------------------------------------------


def test_multiple_stuck_tasks_all_recovered_together(db_session):
    t1 = make_running_task(db_session, age_seconds=200, queued_from_status=TaskStatus.APPROVED, application="a1")
    t2 = make_running_task(
        db_session, age_seconds=300, rollback_target_task_id=999, updatefor="r", application="a2"
    )
    t3 = make_running_task(db_session, age_seconds=150, queued_from_status=TaskStatus.TASK, application="a3")

    recovered = task_service.recover_stuck_running_tasks(db_session)

    assert {t.id for t in recovered} == {t1.id, t2.id, t3.id}
    db_session.refresh(t1)
    db_session.refresh(t2)
    db_session.refresh(t3)
    assert t1.status == TaskStatus.APPROVED
    assert t2.status == TaskStatus.REJECTED
    assert t3.status == TaskStatus.TASK


def test_one_task_failing_during_recovery_does_not_block_the_rest(db_session, monkeypatch):
    """Ép 1 task lỗi (raise) ngay trong vòng lặp phục hồi - task khác vẫn phải được xử lý
    bình thường, không được để 1 exception ngoài dự kiến chặn cả job."""
    healthy = make_running_task(db_session, age_seconds=200, queued_from_status=TaskStatus.APPROVED, application="ok")
    failing = make_running_task(db_session, age_seconds=200, queued_from_status=TaskStatus.APPROVED, application="bad")

    original_cas_update = task_service._cas_update

    def flaky_cas_update(db, task_id, status_condition, values):
        if task_id == failing.id:
            raise RuntimeError("boom - loi ngoai du kien luc phuc hoi")
        return original_cas_update(db, task_id, status_condition, values)

    monkeypatch.setattr(task_service, "_cas_update", flaky_cas_update)

    recovered = task_service.recover_stuck_running_tasks(db_session)

    assert [t.id for t in recovered] == [healthy.id]
    db_session.refresh(healthy)
    assert healthy.status == TaskStatus.APPROVED
    db_session.refresh(failing)
    assert failing.status == TaskStatus.RUNNING, "Task loi ngoai du kien phai giu nguyen RUNNING, khong duoc de o trang thai lung lung"


def test_recovery_exception_does_not_leave_db_session_broken_for_next_task(db_session, monkeypatch, caplog):
    """Sau khi 1 task raise exception, code phải db.rollback() để session còn dùng được
    cho các task tiếp theo trong cùng vòng lặp (không chỉ test riêng lẻ mà còn test dùng
    chung session xuyên suốt các assertion phía trên)."""
    caplog.set_level(logging.WARNING)
    failing = make_running_task(db_session, age_seconds=200, queued_from_status=TaskStatus.APPROVED, application="bad")
    healthy = make_running_task(db_session, age_seconds=200, queued_from_status=TaskStatus.APPROVED, application="ok")

    original_cas_update = task_service._cas_update

    def flaky_cas_update(db, task_id, status_condition, values):
        if task_id == failing.id:
            raise RuntimeError("boom")
        return original_cas_update(db, task_id, status_condition, values)

    monkeypatch.setattr(task_service, "_cas_update", flaky_cas_update)

    recovered = task_service.recover_stuck_running_tasks(db_session)

    assert [t.id for t in recovered] == [healthy.id]
    # Session vẫn dùng được bình thường sau exception + rollback bên trong hàm.
    db_session.refresh(healthy)
    assert healthy.status == TaskStatus.APPROVED


# ---------------------------------------------------------------------------
# 8. Gửi Telegram lỗi -> KHÔNG làm hỏng việc phục hồi đã commit (test wrapper trong main.py)
# ---------------------------------------------------------------------------


def test_startup_wrapper_swallows_telegram_failure_after_recovery_committed(monkeypatch, caplog):
    """app.main._recover_stuck_running_tasks_on_startup(): recover_stuck_running_tasks đã
    commit xong TRƯỚC khi gửi Telegram - nếu gửi Telegram raise, hàm KHÔNG được để lỗi đó
    lan ra ngoài (sẽ chặn app khởi động)."""
    import app.main as main_module

    fake_task = DeployTask(
        id=1,
        project="core",
        application="api",
        commitid="abc1234",
        email="owner@example.com",
        status=TaskStatus.APPROVED,
    )

    monkeypatch.setattr(main_module.task_service, "recover_stuck_running_tasks", lambda db: [fake_task])

    def boom_send_telegram(*a, **k):
        raise ConnectionError("Telegram API khong ket noi duoc")

    monkeypatch.setattr(main_module, "send_telegram", boom_send_telegram)

    caplog.set_level(logging.WARNING)
    # Không được raise ra ngoài.
    main_module._recover_stuck_running_tasks_on_startup()

    assert any("Telegram" in rec.message or "telegram" in rec.message.lower() for rec in caplog.records), (
        "Loi Telegram phai duoc log lai (khong nuot im lang hoan toan)"
    )


def test_startup_wrapper_swallows_unexpected_db_error_and_does_not_block_app_start(monkeypatch, caplog):
    """Lỗi ngoài dự kiến (vd DB tạm thời không kết nối được) trong recover_stuck_running_tasks
    KHÔNG được phép chặn app khởi động."""
    import app.main as main_module

    def boom(db):
        raise RuntimeError("DB tam thoi khong ket noi duoc")

    monkeypatch.setattr(main_module.task_service, "recover_stuck_running_tasks", boom)

    caplog.set_level(logging.WARNING)
    main_module._recover_stuck_running_tasks_on_startup()  # không raise

    assert any(rec.levelno >= logging.WARNING for rec in caplog.records)


# ---------------------------------------------------------------------------
# Bonus - tự kiểm chứng (KHÔNG tin lời dev): updated_at có thật sự được CAS ghi mới lúc
# QUEUED -> RUNNING hay không? Nếu không, toàn bộ ngưỡng 120s vô nghĩa.
# ---------------------------------------------------------------------------


def test_updated_at_is_bumped_by_cas_on_queued_to_running_transition(db_session, monkeypatch):
    stale_at = datetime.utcnow() - timedelta(seconds=500)
    task = DeployTask(
        project="core",
        application="api",
        commitid="abc1234",
        email="owner@example.com",
        status=TaskStatus.QUEUED,
        queued_from_status=TaskStatus.APPROVED,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    from sqlalchemy import update as sa_update

    db_session.execute(sa_update(DeployTask).where(DeployTask.id == task.id).values(updated_at=stale_at))
    db_session.commit()
    db_session.refresh(task)
    assert task.updated_at == stale_at

    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: task_service.git_service.RunResult(ok=True, message="ok", image_version="v1"),
    )

    before_call = datetime.utcnow()
    task_service.run_queued_task(db_session, task.id)
    db_session.refresh(task)

    assert task.updated_at >= before_call, (
        "updated_at PHAI duoc CAS ghi moi ngay luc QUEUED->RUNNING (task_service.run_queued_task goi "
        "_cas_update, tu dong setdefault updated_at=utcnow()) - neu khong, moc thoi gian dung de tinh "
        "'ket > 120s' luc phuc hoi se SAI hoan toan (van la thoi diem enqueue, khong phai luc thuc su "
        "bat dau chay git)."
    )


# ---------------------------------------------------------------------------
# Thứ tự lúc khởi động: recover PHẢI chạy TRƯỚC start_scheduler() (nếu không,
# job_process_task_queue có thể đã đăng ký và đụng độ với chính quét phục hồi này -
# xem docstring recover_stuck_running_tasks).
# ---------------------------------------------------------------------------


def test_lifespan_calls_recover_before_start_scheduler(monkeypatch):
    import asyncio

    import app.main as main_module

    call_order: list[str] = []
    monkeypatch.setattr(
        main_module,
        "_recover_stuck_running_tasks_on_startup",
        lambda: call_order.append("recover"),
    )
    monkeypatch.setattr(main_module, "start_scheduler", lambda: call_order.append("start_scheduler"))
    monkeypatch.setattr(main_module, "shutdown_scheduler", lambda: call_order.append("shutdown_scheduler"))

    async def _run_lifespan_once():
        async with main_module.lifespan(main_module.app):
            pass

    asyncio.run(_run_lifespan_once())

    assert call_order == ["recover", "start_scheduler", "shutdown_scheduler"], (
        "recover_stuck_running_tasks_on_startup PHAI chay TRUOC start_scheduler() - neu khong, "
        "job_process_task_queue co the da duoc dang ky va dang xu ly QUEUED->RUNNING dung luc "
        "quet phuc hoi chay, gay xung dot ngay trong CUNG 1 process (xem docstring "
        "task_service.recover_stuck_running_tasks)."
    )
