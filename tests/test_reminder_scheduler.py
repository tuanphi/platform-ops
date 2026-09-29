"""Kiểm thử các job nhắc nhở dùng field `confirmed_run_at` (nay được nhập ở bước
Create, không phải Approve - xem app/routers/tasks_router.py::create_submit và
app/services/task_service.py::create_task) trong app/scheduler.py.

Gọi thẳng hàm job (không chờ APScheduler chạy thật), patch SessionLocal của module
scheduler trỏ về db_session in-memory của test, và patch send_telegram/send_mail để
không gọi mạng thật - theo đúng convention cô lập DB/network của tests/conftest.py.
"""

from datetime import datetime, timedelta

from app import scheduler as scheduler_module
from app.models import DeployTask, TaskStatus
from app.services import app_setting_service


def make_task(db_session, status=TaskStatus.APPROVED, **kwargs):
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


def _patch_scheduler_session(monkeypatch, db_session):
    """Job tự mở/đóng session riêng qua SessionLocal() - trỏ thẳng về db_session của
    test (không tạo session mới) để assertion sau khi job chạy đọc được đúng dữ liệu
    đã commit, dùng chung 1 connection SQLite in-memory (StaticPool, xem conftest.py)."""
    monkeypatch.setattr(scheduler_module, "SessionLocal", lambda: db_session)


# ---------------------------------------------------------------------------
# job_remind_approved_now - nhắc "Deploy Now" khi confirmed_run_at trong +-3 phút
# ---------------------------------------------------------------------------


def test_job_remind_approved_now_notifies_task_within_window(db_session, monkeypatch):
    _patch_scheduler_session(monkeypatch, db_session)
    sent = []
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: sent.append(msg))
    monkeypatch.setattr(scheduler_module, "send_mail", lambda *a, **k: None)

    task = make_task(
        db_session,
        status=TaskStatus.APPROVED,
        confirmed_run_at=datetime.utcnow(),
        notify_status=0,
    )
    task_id = task.id

    scheduler_module.job_remind_approved_now()

    assert len(sent) == 1
    assert "Need Deploy Now" in sent[0]
    # session.close() trong job da detach object cu - can query lai thay vi refresh()
    reloaded = db_session.get(DeployTask, task_id)
    assert reloaded.notify_status == 2
    assert reloaded.run_at is not None


def test_job_remind_approved_now_skips_task_outside_window(db_session, monkeypatch):
    _patch_scheduler_session(monkeypatch, db_session)
    sent = []
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: sent.append(msg))

    task = make_task(
        db_session,
        status=TaskStatus.APPROVED,
        confirmed_run_at=datetime.utcnow() + timedelta(hours=2),
        notify_status=0,
    )
    task_id = task.id

    scheduler_module.job_remind_approved_now()

    assert sent == []
    reloaded = db_session.get(DeployTask, task_id)
    assert reloaded.notify_status == 0  # không bị đổi vì chưa tới cửa sổ nhắc


def test_job_remind_approved_now_skips_task_already_notified_twice(db_session, monkeypatch):
    """notify_status == 2 nghĩa là đã nhắc lần 'Deploy Now' rồi - job không nhắc lại
    dù confirmed_run_at vẫn còn trong cửa sổ +-3 phút (tránh spam Telegram mỗi phút)."""
    _patch_scheduler_session(monkeypatch, db_session)
    sent = []
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: sent.append(msg))

    make_task(
        db_session,
        status=TaskStatus.APPROVED,
        confirmed_run_at=datetime.utcnow(),
        notify_status=2,
    )

    scheduler_module.job_remind_approved_now()

    assert sent == []


def test_job_remind_approved_now_ignores_non_approved_task(db_session, monkeypatch):
    """Task còn ở status Task (chưa Approve) dù confirmed_run_at trong cửa sổ vẫn
    không được nhắc 'Deploy Now' - nhắc nhở chỉ áp dụng cho task đã Approve."""
    _patch_scheduler_session(monkeypatch, db_session)
    sent = []
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: sent.append(msg))

    make_task(
        db_session,
        status=TaskStatus.TASK,
        confirmed_run_at=datetime.utcnow(),
        notify_status=0,
    )

    scheduler_module.job_remind_approved_now()

    assert sent == []


# ---------------------------------------------------------------------------
# job_remind_30min_before - nhắc trước 30 phút (mặc định TẮT qua AppSetting)
# ---------------------------------------------------------------------------


def test_job_remind_30min_before_disabled_by_default_does_not_notify(db_session, monkeypatch):
    _patch_scheduler_session(monkeypatch, db_session)
    sent = []
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: sent.append(msg))

    make_task(
        db_session,
        status=TaskStatus.APPROVED,
        confirmed_run_at=datetime.utcnow() + timedelta(minutes=10),
        notify_status=0,
    )

    scheduler_module.job_remind_30min_before()

    assert sent == []  # enable_30min_reminder mac dinh False (app/config.py)


def test_job_remind_30min_before_notifies_when_enabled_and_within_window(db_session, monkeypatch):
    _patch_scheduler_session(monkeypatch, db_session)
    app_setting_service.update_app_setting(db_session, enable_30min_reminder=True)
    sent = []
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: sent.append(msg))

    task = make_task(
        db_session,
        status=TaskStatus.APPROVED,
        confirmed_run_at=datetime.utcnow() + timedelta(minutes=10),
        notify_status=0,
    )
    task_id = task.id

    scheduler_module.job_remind_30min_before()

    assert len(sent) == 1
    assert "30 phút" in sent[0]
    reloaded = db_session.get(DeployTask, task_id)
    assert reloaded.notify_status == 1


def test_job_remind_30min_before_skips_task_outside_30min_window(db_session, monkeypatch):
    _patch_scheduler_session(monkeypatch, db_session)
    app_setting_service.update_app_setting(db_session, enable_30min_reminder=True)
    sent = []
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: sent.append(msg))

    task = make_task(
        db_session,
        status=TaskStatus.APPROVED,
        confirmed_run_at=datetime.utcnow() + timedelta(hours=5),
        notify_status=0,
    )
    task_id = task.id

    scheduler_module.job_remind_30min_before()

    assert sent == []
    reloaded = db_session.get(DeployTask, task_id)
    assert reloaded.notify_status == 0


def test_job_remind_30min_before_skips_task_already_notified(db_session, monkeypatch):
    _patch_scheduler_session(monkeypatch, db_session)
    app_setting_service.update_app_setting(db_session, enable_30min_reminder=True)
    sent = []
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: sent.append(msg))

    make_task(
        db_session,
        status=TaskStatus.APPROVED,
        confirmed_run_at=datetime.utcnow() + timedelta(minutes=10),
        notify_status=1,
    )

    scheduler_module.job_remind_30min_before()

    assert sent == []
