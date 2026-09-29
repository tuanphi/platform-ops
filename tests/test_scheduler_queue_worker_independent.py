"""Chốt chặn regression NGHIÊM TRỌNG: worker hàng đợi Run/Rollback
(`job_process_task_queue`) PHẢI luôn chạy độc lập với công tắc
`AppSetting.enable_scheduler` - nếu không, admin tắt công tắc đó là mọi
Run/Rollback kẹt vĩnh viễn ở `Queued` mà không có cảnh báo (xem
app/scheduler.py::_OPTIONAL_JOB_IDS/_add_optional_jobs/_remove_optional_jobs/
apply_scheduler_setting/start_scheduler).

Mỗi test dùng 1 `BackgroundScheduler` MỚI, tự gắn vào `scheduler_module.scheduler`
qua monkeypatch (tự động revert khi test kết thúc) - KHÔNG bao giờ đụng vào
singleton scheduler thật của app (vốn không được khởi động trong test, xem
tests/conftest.py::client_factory patch start_scheduler/shutdown_scheduler thành
no-op) - tự shutdown scheduler tạm ở cuối mỗi test để không rò rỉ thread/job
sang test khác.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from app import scheduler as scheduler_module

OPTIONAL_JOB_IDS = [
    "remind_approved_now",
    "check_running_done",
    "check_running_too_long",
    "auto_deploy",
    "auto_sync_catalog",
    "remind_30min_before",
    "sync_env_apps",
]
QUEUE_JOB_ID = "process_task_queue"


@pytest.fixture()
def fresh_scheduler(monkeypatch):
    """Gắn 1 BackgroundScheduler mới, riêng biệt cho test này vào tên module-level
    `scheduler_module.scheduler` - các hàm trong app/scheduler.py tra cứu biến toàn
    cục `scheduler` tại thời điểm gọi (không bind cứng lúc định nghĩa) nên việc
    monkeypatch này có hiệu lực xuyên suốt start_scheduler/apply_scheduler_setting/
    shutdown_scheduler. Tự shutdown ở teardown, tự revert biến qua monkeypatch."""
    test_scheduler = BackgroundScheduler(timezone="UTC")
    monkeypatch.setattr(scheduler_module, "scheduler", test_scheduler)
    yield test_scheduler
    if test_scheduler.running:
        test_scheduler.shutdown(wait=False)


def _patch_enable_scheduler(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(
        scheduler_module, "get_app_setting", lambda *a, **k: SimpleNamespace(enable_scheduler=value)
    )


def _job_ids(test_scheduler: BackgroundScheduler) -> set[str]:
    return {job.id for job in test_scheduler.get_jobs()}


# ---------------------------------------------------------------------------
# 1+2. enable_scheduler=False luc start_scheduler(): queue worker VAN chay,
#      7 job phu KHONG duoc dang ky.
# ---------------------------------------------------------------------------


def test_start_scheduler_disabled_keeps_queue_worker_running(fresh_scheduler, monkeypatch):
    _patch_enable_scheduler(monkeypatch, False)

    scheduler_module.start_scheduler()

    assert fresh_scheduler.running is True
    assert fresh_scheduler.get_job(QUEUE_JOB_ID) is not None, (
        "process_task_queue PHAI luon duoc dang ky bat ke enable_scheduler - neu khong "
        "moi Run/Rollback se ket vinh vien o Queued khi admin tat cong tac nay"
    )


def test_start_scheduler_disabled_skips_optional_jobs(fresh_scheduler, monkeypatch):
    _patch_enable_scheduler(monkeypatch, False)

    scheduler_module.start_scheduler()

    registered = _job_ids(fresh_scheduler)
    for job_id in OPTIONAL_JOB_IDS:
        assert job_id not in registered, f"Job phu '{job_id}' khong duoc dang ky khi enable_scheduler=False"


# ---------------------------------------------------------------------------
# 3. enable_scheduler=True: co du ca queue worker lan 7 job phu.
# ---------------------------------------------------------------------------


def test_start_scheduler_enabled_registers_queue_worker_and_all_optional_jobs(fresh_scheduler, monkeypatch):
    _patch_enable_scheduler(monkeypatch, True)

    scheduler_module.start_scheduler()

    registered = _job_ids(fresh_scheduler)
    assert QUEUE_JOB_ID in registered
    for job_id in OPTIONAL_JOB_IDS:
        assert job_id in registered, f"Job phu '{job_id}' phai duoc dang ky khi enable_scheduler=True"
    assert registered == set(OPTIONAL_JOB_IDS) | {QUEUE_JOB_ID}


# ---------------------------------------------------------------------------
# 4. Bat/tat qua apply_scheduler_setting(): tat -> go dung 7 job phu, queue worker
#    KHONG bi go; bat lai -> 7 job phu quay lai day du, queue worker khong bi anh
#    huong xuyen suot (kiem tra ca job instance khong bi thay doi/tao lai).
# ---------------------------------------------------------------------------


def test_apply_scheduler_setting_toggle_off_removes_only_optional_jobs(fresh_scheduler, monkeypatch):
    _patch_enable_scheduler(monkeypatch, True)
    scheduler_module.start_scheduler()
    assert _job_ids(fresh_scheduler) == set(OPTIONAL_JOB_IDS) | {QUEUE_JOB_ID}

    _patch_enable_scheduler(monkeypatch, False)
    scheduler_module.apply_scheduler_setting()

    registered = _job_ids(fresh_scheduler)
    assert registered == {QUEUE_JOB_ID}, "Tat cong tac phai go DUNG 7 job phu, khong dung gi khac"
    assert fresh_scheduler.running is True, "apply_scheduler_setting KHONG duoc dung ca scheduler"


def test_apply_scheduler_setting_toggle_on_again_restores_all_optional_jobs(fresh_scheduler, monkeypatch):
    _patch_enable_scheduler(monkeypatch, True)
    scheduler_module.start_scheduler()

    _patch_enable_scheduler(monkeypatch, False)
    scheduler_module.apply_scheduler_setting()
    assert _job_ids(fresh_scheduler) == {QUEUE_JOB_ID}

    _patch_enable_scheduler(monkeypatch, True)
    scheduler_module.apply_scheduler_setting()

    registered = _job_ids(fresh_scheduler)
    assert registered == set(OPTIONAL_JOB_IDS) | {QUEUE_JOB_ID}, "Bat lai phai co du 7 job phu"


def test_apply_scheduler_setting_toggle_off_on_multiple_times_queue_worker_never_removed(
    fresh_scheduler, monkeypatch
):
    """Bat/tat lap lai nhieu lan (mo phong admin bam qua lai nut) - process_task_queue
    khong duoc go/dang ky lai (van la CUNG 1 job instance, khong bi mat lich trinh)."""
    _patch_enable_scheduler(monkeypatch, True)
    scheduler_module.start_scheduler()
    original_queue_job = fresh_scheduler.get_job(QUEUE_JOB_ID)
    assert original_queue_job is not None

    for enabled in (False, True, False, True, False):
        _patch_enable_scheduler(monkeypatch, enabled)
        scheduler_module.apply_scheduler_setting()
        queue_job = fresh_scheduler.get_job(QUEUE_JOB_ID)
        assert queue_job is not None, "process_task_queue bien mat sau khi toggle enable_scheduler"
        assert queue_job.next_run_time == original_queue_job.next_run_time, (
            "process_task_queue bi thay the/tao lai job moi thay vi giu nguyen lich trinh"
        )


# ---------------------------------------------------------------------------
# Guard `if not scheduler.running: return` trong apply_scheduler_setting() - khong
# duoc crash khi scheduler chua/khong chay (vd goi truoc start_scheduler, hoac
# scheduler that da bi dung ngoai y muon) - va KHONG duoc lam gi ca trong truong
# hop nay (khong dang ky nham job vao 1 scheduler chua chay).
# ---------------------------------------------------------------------------


def test_apply_scheduler_setting_noop_when_scheduler_not_running(fresh_scheduler, monkeypatch):
    called = []
    monkeypatch.setattr(scheduler_module, "get_app_setting", lambda *a, **k: called.append(1))

    assert fresh_scheduler.running is False
    scheduler_module.apply_scheduler_setting()  # khong duoc raise

    assert called == [], "Guard phai return SOM, khong duoc doc AppSetting khi scheduler chua chay"
    assert fresh_scheduler.get_jobs() == []


def test_start_scheduler_called_twice_is_idempotent_and_reapplies_setting(fresh_scheduler, monkeypatch):
    """start_scheduler() goi lan 2 (vd do loi lifespan) khong duoc lam vo hieu qua
    scheduler.start() lan 2 - phai tu phat hien dang chay va chi goi lai
    apply_scheduler_setting() (xem docstring start_scheduler)."""
    _patch_enable_scheduler(monkeypatch, True)
    scheduler_module.start_scheduler()
    assert _job_ids(fresh_scheduler) == set(OPTIONAL_JOB_IDS) | {QUEUE_JOB_ID}

    _patch_enable_scheduler(monkeypatch, False)
    scheduler_module.start_scheduler()  # goi lan 2

    assert fresh_scheduler.running is True
    assert _job_ids(fresh_scheduler) == {QUEUE_JOB_ID}, "Lan goi start_scheduler thu 2 phai dong bo lai theo setting"
