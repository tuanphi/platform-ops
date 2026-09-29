"""Test POST /settings/general (section="scheduler") - hien khong co test nao cover
nhanh nay (xem app/routers/settings_router.py, nhanh `elif section == "scheduler"`
goi apply_scheduler_setting() thay vi start_scheduler()/shutdown_scheduler()).

Trong client_factory (tests/conftest.py) main_module.start_scheduler bi monkeypatch
thanh no-op nen scheduler that (app.scheduler.scheduler) khong bao gio chay trong
test - apply_scheduler_setting() se early-return qua guard `if not scheduler.running`
va KHONG dong bo job nao ca. Test o day vi vay CHI xac nhan phan luu DB (round-trip
gia tri) + route khong crash, KHONG the xac nhan hieu ung dang ky/go job qua HTTP
(da duoc cover truc tiep o tests/test_scheduler_queue_worker_independent.py)."""

from __future__ import annotations

from app import scheduler as scheduler_module
from app.models import Role
from app.services.app_setting_service import get_app_setting
from tests.conftest import make_user


def test_post_scheduler_section_enable_true_saves_and_calls_apply_scheduler_setting(
    client_factory, db_session, monkeypatch
):
    sa = make_user(db_session, email="sa-sched-on@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)

    calls = []
    monkeypatch.setattr(scheduler_module, "apply_scheduler_setting", lambda: calls.append(1))
    import app.routers.settings_router as settings_router_module

    monkeypatch.setattr(settings_router_module, "apply_scheduler_setting", lambda: calls.append(1))

    resp = client.post(
        "/settings/general",
        data={"section": "scheduler", "enable_scheduler": "on", "enable_30min_reminder": "on"},
        headers={"accept": "application/json"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    updated = get_app_setting(db_session)
    assert updated.enable_scheduler is True
    assert updated.enable_30min_reminder is True
    assert calls == [1], "section=scheduler phai goi apply_scheduler_setting() (khong phai start/shutdown_scheduler)"
    # Trong moi test o file nay, scheduler that (scheduler_module.scheduler) khong bao gio
    # duoc .start() (xem client_factory patch start_scheduler thanh no-op) - router tu kiem
    # tra scheduler_module.scheduler.running SAU KHI goi apply_scheduler_setting() (o day da
    # bi monkeypatch thanh no-op) nen luon roi vao nhanh canh bao. ok=True (gia tri DA luu
    # dung vao DB) nhung message PHAI neu ro scheduler/worker Run-Rollback chua nhan cau hinh.
    assert "KHÔNG chạy" in body["message"] or "khong chay" in body["message"].lower()
    assert "worker Run/Rollback" in body["message"]


def test_post_scheduler_section_enable_false_saves_immediately_without_restart(client_factory, db_session):
    sa = make_user(db_session, email="sa-sched-off@example.com", role=Role.SUPER_ADMIN)
    # Bat truoc de co gia tri True ban dau, xac nhan tat di lam thay doi that.
    from app.services.app_setting_service import update_app_setting

    update_app_setting(db_session, enable_scheduler=True, enable_30min_reminder=True)
    client = client_factory(user=sa)

    resp = client.post(
        "/settings/general",
        data={"section": "scheduler"},  # checkbox khong duoc gui = tat (unchecked)
        headers={"accept": "application/json"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    updated = get_app_setting(db_session)
    assert updated.enable_scheduler is False, "Khong gui checkbox -> phai luu thanh False (unchecked)"
    assert updated.enable_30min_reminder is False
    # Scheduler that khong chay trong test (xem test truoc) -> message phai canh bao ro.
    assert "KHÔNG chạy" in body["message"] or "khong chay" in body["message"].lower()


def test_post_scheduler_section_round_trip_toggle_on_off_on(client_factory, db_session):
    """Bat -> tat -> bat lai qua UI, gia tri luu DB phai dung tung buoc, khong can
    restart app (khong co assertion nao yeu cau restart o day)."""
    sa = make_user(db_session, email="sa-sched-roundtrip@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)

    resp1 = client.post(
        "/settings/general",
        data={"section": "scheduler", "enable_scheduler": "on"},
        headers={"accept": "application/json"},
    )
    assert resp1.json()["ok"] is True
    assert get_app_setting(db_session).enable_scheduler is True
    assert "khong chay" in resp1.json()["message"].lower() or "KHÔNG chạy" in resp1.json()["message"]

    resp2 = client.post(
        "/settings/general",
        data={"section": "scheduler"},
        headers={"accept": "application/json"},
    )
    assert resp2.json()["ok"] is True
    assert get_app_setting(db_session).enable_scheduler is False
    assert "khong chay" in resp2.json()["message"].lower() or "KHÔNG chạy" in resp2.json()["message"]

    resp3 = client.post(
        "/settings/general",
        data={"section": "scheduler", "enable_scheduler": "on"},
        headers={"accept": "application/json"},
    )
    assert resp3.json()["ok"] is True
    assert get_app_setting(db_session).enable_scheduler is True
    assert "khong chay" in resp3.json()["message"].lower() or "KHÔNG chạy" in resp3.json()["message"]


def test_post_scheduler_section_no_warning_when_scheduler_actually_running(client_factory, db_session, monkeypatch):
    """Doi chung voi cac test tren: khi scheduler THAT SU dang chay (gan 1
    BackgroundScheduler tam thoi vao app.scheduler.scheduler), message tra ve KHONG
    duoc chua canh bao "khong chay" - tranh bao dong gia lam admin hoang mang khi moi
    thu van hoat dong binh thuong."""
    from apscheduler.schedulers.background import BackgroundScheduler

    import app.scheduler as scheduler_module

    test_scheduler = BackgroundScheduler(timezone="UTC")
    test_scheduler.start()
    monkeypatch.setattr(scheduler_module, "scheduler", test_scheduler)
    # apply_scheduler_setting() goi get_app_setting() KHONG truyen db -> tu mo session rieng
    # toi engine THAT (app.database.engine), khac han engine in-memory rieng cua db_session
    # fixture (chua co bang) - patch ham nay giong tests/test_scheduler_queue_worker_independent.py
    # (_patch_enable_scheduler) de tranh loi "no such table" khong lien quan gi toi thu dang test.
    from types import SimpleNamespace

    monkeypatch.setattr(scheduler_module, "get_app_setting", lambda *a, **k: SimpleNamespace(enable_scheduler=True))
    try:
        sa = make_user(db_session, email="sa-sched-running@example.com", role=Role.SUPER_ADMIN)
        client = client_factory(user=sa)

        resp = client.post(
            "/settings/general",
            data={"section": "scheduler", "enable_scheduler": "on"},
            headers={"accept": "application/json"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "khong chay" not in body["message"].lower()
        assert "KHÔNG chạy" not in body["message"]
    finally:
        if test_scheduler.running:
            test_scheduler.shutdown(wait=False)


def test_post_scheduler_section_requires_settings_access(client_factory, db_session):
    """User thuong (khong phai Admin/Super Admin) khong duoc phep sua Scheduler
    setting - dung require_settings_access (xem app/auth.py)."""
    user = make_user(db_session, email="user-sched@example.com", role=Role.USER)
    client = client_factory(user=user)

    resp = client.post(
        "/settings/general",
        data={"section": "scheduler", "enable_scheduler": "on"},
        headers={"accept": "application/json"},
    )

    assert resp.status_code in (302, 303, 401, 403), "User thuong khong duoc phep doi Scheduler setting"


def test_general_page_renders_updated_scheduler_switch_description(client_factory, db_session):
    """Regression: mo ta cong tac o settings_general.html da doi (dev vua sua) - phai
    noi ro cong tac nay KHONG anh huong worker xu ly hang doi Run/Rollback."""
    sa = make_user(db_session, email="sa-sched-html@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)

    resp = client.get("/settings/general")

    assert resp.status_code == 200
    assert (
        "KHÔNG ảnh hưởng việc xử lý hàng đợi Run/Rollback" in resp.text
    ), "Mo ta cong tac Scheduler phai neu ro khong anh huong worker hang doi Run/Rollback"
    assert "task Queued vẫn luôn được chạy dù tắt scheduler" in resp.text
