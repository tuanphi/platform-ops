"""Chốt chặn: apply_scheduler_setting() không còn lỗi im lặng khi scheduler thật sự
không chạy (xem app/scheduler.py dòng ~346-373) - phải log WARNING rõ ràng để phát hiện
qua log/alerting, thay vì early-return không dấu vết như trước.

Dùng `caplog`, KHÔNG khởi động scheduler thật của app (singleton `scheduler_module.scheduler`
không được .start() ở test "not running"; test "running" dùng 1 BackgroundScheduler tạm
thời riêng, theo đúng pattern `fresh_scheduler` của tests/test_scheduler_queue_worker_independent.py)."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from app import scheduler as scheduler_module


def test_apply_scheduler_setting_logs_warning_when_scheduler_not_running(monkeypatch, caplog):
    # Đảm bảo chắc chắn KHÔNG chạy (không phụ thuộc thứ tự test khác vô tình .start()).
    assert scheduler_module.scheduler.running is False

    caplog.set_level(logging.WARNING, logger="app.scheduler")
    scheduler_module.apply_scheduler_setting()  # không raise

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "apply_scheduler_setting() phai log WARNING khi scheduler khong chay"
    assert "process_task_queue" in warnings[0].message or "hang doi" in warnings[0].message.lower(), (
        "Message WARNING phai neu ro worker hang doi Run/Rollback cung dang KHONG chay, khong chi noi chung chung"
    )


def test_apply_scheduler_setting_does_not_raise_when_scheduler_not_running(monkeypatch, caplog):
    """Guard phải return sớm (không được thử add_job/remove_job trên scheduler chưa chạy
    rồi raise lỗi APScheduler khác che mất nguyên nhân gốc)."""
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(scheduler_module, "get_app_setting", lambda *a, **k: SimpleNamespace(enable_scheduler=True))

    scheduler_module.apply_scheduler_setting()  # không raise SchedulerNotRunningError/khác


@pytest.fixture()
def fresh_running_scheduler(monkeypatch):
    """1 BackgroundScheduler tạm thời, THẬT SỰ .start() được, gắn vào scheduler_module.scheduler
    để scheduler.running trả về True mà không đụng tới singleton thật của app."""
    test_scheduler = BackgroundScheduler(timezone="UTC")
    test_scheduler.start()
    monkeypatch.setattr(scheduler_module, "scheduler", test_scheduler)
    yield test_scheduler
    if test_scheduler.running:
        test_scheduler.shutdown(wait=False)


def test_apply_scheduler_setting_no_warning_when_scheduler_actually_running(
    fresh_running_scheduler, monkeypatch, caplog
):
    monkeypatch.setattr(scheduler_module, "get_app_setting", lambda *a, **k: SimpleNamespace(enable_scheduler=True))

    caplog.set_level(logging.WARNING, logger="app.scheduler")
    scheduler_module.apply_scheduler_setting()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [], "Khong duoc canh bao gia khi scheduler THAT SU dang chay"
