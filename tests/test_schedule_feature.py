"""Menu Schedule: tinh lan chay ke tiep, tao lich qua router, job chay lich den gio."""

from datetime import datetime
from types import SimpleNamespace

from app.models import AppSchedule, EnvAppCache, Role
from app.services import argocd_service, env_toggle_service, schedule_service, staging_service
from app.timeutil import utc_to_gmt7


def _sched(**kw) -> AppSchedule:
    return AppSchedule(**kw)


def test_compute_next_run_daily_weekly_monthly_once():
    # 2026-10-16 (Thu 6) 03:00 UTC = 10:00 GMT+7
    now = datetime(2026, 10, 16, 3, 0)
    # daily 09:00 VN da qua -> ngay mai 09:00 VN = 02:00 UTC
    assert schedule_service.compute_next_run(_sched(frequency="daily", time_of_day="09:00"), now) == datetime(2026, 10, 17, 2, 0)
    # daily 11:00 VN chua toi -> hom nay
    assert schedule_service.compute_next_run(_sched(frequency="daily", time_of_day="11:00"), now) == datetime(2026, 10, 16, 4, 0)
    # weekly Thu 2 (0) 08:00 VN -> 19/10
    assert schedule_service.compute_next_run(_sched(frequency="weekly", time_of_day="08:00", weekday=0), now) == datetime(2026, 10, 19, 1, 0)
    # monthly ngay 31 -> 31/10; tu 31/10 sau gio chay -> 30/11 (thang 11 khong co ngay 31)
    m = _sched(frequency="monthly", time_of_day="08:00", day_of_month=31)
    assert schedule_service.compute_next_run(m, now) == datetime(2026, 10, 31, 1, 0)
    assert schedule_service.compute_next_run(m, datetime(2026, 10, 31, 2, 0)) == datetime(2026, 11, 30, 1, 0)
    # once da qua -> None
    once = _sched(frequency="once", run_once_at=datetime(2026, 10, 16, 2, 0))
    assert schedule_service.compute_next_run(once, now) is None


def test_create_schedule_via_router_and_list(client_factory, make_user_factory, db_session):
    user = make_user_factory(role=Role.SUPER_ADMIN, email="sa@example.com")
    db_session.add(EnvAppCache(env="staging", project="grp", application="svc", replicas="1"))
    db_session.commit()
    client = client_factory(user)

    r = client.post(
        "/schedule/create",
        data={"action": "restart", "env": "staging", "project": "grp", "application": "svc",
              "frequency": "weekly", "time_of_day": "22:30", "weekday": "4"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    s = db_session.query(AppSchedule).one()
    assert (s.action, s.frequency, s.weekday, s.time_of_day, s.is_active) == ("restart", "weekly", 4, "22:30", True)
    assert s.next_run_at is not None

    r = client.post(
        "/schedule/create",
        data={"action": "stop", "env": "staging", "project": "grp", "application": "svc",
              "frequency": "once", "run_once_at": "2099-01-01T23:00"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert db_session.query(AppSchedule).count() == 2

    page = client.get("/schedule?tab=restart")
    assert page.status_code == 200 and "grp/svc" in page.text and "Hàng tuần Thứ 6 lúc 22:30" in page.text


def test_run_due_schedules_executes_restart_and_toggle(make_user_factory, db_session, monkeypatch):
    user = make_user_factory(role=Role.SUPER_ADMIN, email="sa@example.com")
    db_session.add(EnvAppCache(env="staging", project="grp", application="svc", replicas="1"))
    now = datetime(2026, 10, 16, 3, 0)
    daily = AppSchedule(action="restart", env="staging", project="grp", application="svc", frequency="daily",
                        time_of_day="10:00", next_run_at=now, is_active=True, created_by_id=user.id, created_by_email=user.email)
    once = AppSchedule(action="stop", env="staging", project="grp", application="svc", frequency="once",
                       run_once_at=now, next_run_at=now, is_active=True, created_by_id=user.id, created_by_email=user.email)
    db_session.add_all([daily, once])
    db_session.commit()

    monkeypatch.setattr(schedule_service.restart_service, "_restart_last_call", {})
    monkeypatch.setattr(schedule_service.notify_service, "send_infra_toggle_telegram", lambda *a, **k: None)
    monkeypatch.setattr(env_toggle_service, "notify", lambda *a, **k: None)
    monkeypatch.setattr(
        argocd_service, "restart_workload",
        lambda db, env, p, a: argocd_service.WorkloadRestartResult(ok=True, message="restarted"),
    )
    toggled = []
    monkeypatch.setattr(staging_service, "_push_config", lambda db: None)
    monkeypatch.setattr(
        staging_service.git_service, "toggle_staging_app",
        lambda p, a, r, email, cfg: toggled.append((p, a, r)) or SimpleNamespace(ok=True, message="ok", changed=((p, a),), pending_merge_request=False),
    )

    assert schedule_service.run_due_schedules(db_session, now=now) == 2

    db_session.refresh(daily)
    db_session.refresh(once)
    assert daily.last_ok is True and daily.is_active and daily.next_run_at == datetime(2026, 10, 17, 3, 0)
    assert once.last_ok is True and once.is_active is False and once.next_run_at is None
    assert toggled == [("grp", "svc", 0)]
    assert db_session.query(EnvAppCache).one().replicas == "0"


def test_schedule_permission_is_separate_from_staging_restart(client_factory, make_user_factory, db_session):
    db_session.add(EnvAppCache(env="staging", project="grp", application="svc", replicas="1"))
    db_session.commit()
    data = {"action": "restart", "env": "staging", "project": "grp", "application": "svc",
            "frequency": "daily", "time_of_day": "01:00"}

    # Co quyen Restart/Replicas menu Staging nhung KHONG co quyen Schedule -> bi chan
    restarter = make_user_factory(email="r@example.com", can_toggle_restart_staging=True, can_toggle_staging=True)
    assert not restarter.can_access_schedule
    assert client_factory(restarter).get("/schedule").status_code == 403

    # Chi co quyen Schedule Restart Staging -> tao duoc lich restart staging, khong tao duoc tat/bat
    scheduler_user = make_user_factory(email="s@example.com", can_schedule_restart_staging=True)
    client = client_factory(scheduler_user)
    client.post("/schedule/create", data=data, follow_redirects=False)
    client.post("/schedule/create", data={**data, "action": "stop"}, follow_redirects=False)
    assert [s.action for s in db_session.query(AppSchedule).all()] == ["restart"]


def test_users_page_toggles_schedule_permission(client_factory, make_user_factory, db_session):
    admin = make_user_factory(role=Role.SUPER_ADMIN, email="sa@example.com")
    target = make_user_factory(email="t@example.com")
    client = client_factory(admin)
    r = client.post(f"/users/{target.id}/schedule-toggle/toggle-production", headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"})
    assert r.status_code == 200
    db_session.refresh(target)
    assert target.can_schedule_toggle_production is True
    assert target.can_schedule("production", "stop") and not target.can_schedule("staging", "stop")
    assert "schedule-toggle/toggle-production" in client.get("/users").text


def test_edit_schedule_via_router(client_factory, make_user_factory, db_session):
    user = make_user_factory(role=Role.SUPER_ADMIN, email="sa@example.com")
    db_session.add_all([
        EnvAppCache(env="staging", project="grp", application="svc", replicas="1"),
        EnvAppCache(env="staging", project="grp", application="svc2", replicas="1"),
    ])
    db_session.commit()
    client = client_factory(user)
    base = {"action": "stop", "env": "staging", "project": "grp", "application": "svc",
            "frequency": "daily", "time_of_day": "22:00"}
    client.post("/schedule/create", data=base, follow_redirects=False)
    s = db_session.query(AppSchedule).one()
    assert 'sch-row-edit' in client.get("/schedule?tab=toggle").text

    r = client.post(f"/schedule/{s.id}/edit",
                    data={**base, "application": "svc2", "frequency": "monthly", "time_of_day": "23:15", "day_of_month": "5"},
                    follow_redirects=False)
    assert r.status_code == 303
    db_session.refresh(s)
    assert (s.application, s.frequency, s.time_of_day, s.day_of_month, s.weekday) == ("svc2", "monthly", "23:15", 5, None)
    assert db_session.query(AppSchedule).count() == 1
    assert utc_to_gmt7(s.next_run_at).day == 5


def test_status_endpoint_returns_run_info(client_factory, make_user_factory, db_session):
    user = make_user_factory(role=Role.SUPER_ADMIN, email="sa@example.com")
    s = AppSchedule(action="restart", env="staging", project="grp", application="svc", frequency="daily",
                    time_of_day="10:00", next_run_at=datetime(2026, 10, 17, 3, 0), is_active=True,
                    last_run_at=datetime(2026, 10, 16, 3, 0), last_ok=False, last_message="boom",
                    created_by_id=user.id, created_by_email=user.email)
    db_session.add(s)
    db_session.commit()
    data = client_factory(user).get("/schedule/status?tab=restart").json()
    assert data == [{"id": s.id, "is_active": True, "next_run_at": "17/10/2026 10:00",
                     "last_run_at": "16/10/2026 10:00", "last_ok": False, "last_skipped": False,
                     "last_message": "boom", "finished": False}]


def test_create_schedules_for_multiple_apps(client_factory, make_user_factory, db_session):
    user = make_user_factory(role=Role.SUPER_ADMIN, email="sa@example.com")
    for project, app in [("pay", "api"), ("pay", "worker"), ("wallet", "api")]:
        db_session.add(EnvAppCache(env="staging", project=project, application=app, replicas="1"))
    db_session.commit()
    client = client_factory(user)
    base = {"action": "stop", "env": "staging", "frequency": "daily", "time_of_day": "22:00"}

    client.post("/schedule/create", data={**base, "apps": ["pay|api", "pay|worker", "wallet|api"]}, follow_redirects=False)
    rows = db_session.query(AppSchedule).order_by(AppSchedule.id).all()
    assert [(s.project, s.application, s.time_of_day) for s in rows] == [
        ("pay", "api", "22:00"), ("pay", "worker", "22:00"), ("wallet", "api", "22:00"),
    ]
    assert len({s.next_run_at for s in rows}) == 1

    # Lan 2: 1 app trung lich + 1 app khong ton tai bi bo qua, khong tao them dong nao.
    client.post("/schedule/create", data={**base, "apps": ["pay|api", "ghost|x"]}, follow_redirects=False)
    assert db_session.query(AppSchedule).count() == 3

    # Lan 3: gio khac -> tao moi cho app hop le, bo qua app khong ton tai.
    client.post("/schedule/create", data={**base, "time_of_day": "06:00", "apps": ["pay|api", "ghost|x"]}, follow_redirects=False)
    assert db_session.query(AppSchedule).filter_by(time_of_day="06:00").count() == 1


def test_bulk_pause_resume_delete(client_factory, make_user_factory, db_session):
    user = make_user_factory(email="s@example.com", can_schedule_toggle_staging=True)
    now = datetime.utcnow()

    def add(env, frequency="daily", **kw):
        s = AppSchedule(action="stop", env=env, project="grp", application=f"app{db_session.query(AppSchedule).count()}",
                        frequency=frequency, next_run_at=now, is_active=True,
                        created_by_id=user.id, created_by_email=user.email, **{"time_of_day": "22:00", **kw})
        db_session.add(s)
        db_session.commit()
        return s

    a, b = add("staging"), add("staging")
    once_past = add("staging", frequency="once", run_once_at=datetime(2020, 1, 1), time_of_day=None)
    no_perm = add("production")  # user khong co quyen Schedule Tat/Bat Production
    client = client_factory(user)

    client.post("/schedule/bulk", data={"op": "pause", "tab": "toggle", "ids": [a.id, b.id, once_past.id, no_perm.id]}, follow_redirects=False)
    for s in (a, b, once_past, no_perm):
        db_session.refresh(s)
    assert (a.is_active, b.is_active, once_past.is_active, no_perm.is_active) == (False, False, False, True)

    client.post("/schedule/bulk", data={"op": "resume", "tab": "toggle", "ids": [a.id, once_past.id]}, follow_redirects=False)
    db_session.refresh(a)
    db_session.refresh(once_past)
    assert a.is_active is True and a.next_run_at > now and once_past.is_active is False

    client.post("/schedule/bulk", data={"op": "delete", "tab": "toggle", "ids": [a.id, b.id, no_perm.id]}, follow_redirects=False)
    remaining = {s.id for s in db_session.query(AppSchedule).all()}
    assert remaining == {once_past.id, no_perm.id}


def _toggle_env(monkeypatch, db_session, calls, unchanged=False):
    """Gia lap git toggle staging: ghi nhan (app, replicas), tra 'khong thay doi' neu unchanged."""
    from app.services import git_service, notify_service
    sent = []
    monkeypatch.setattr(notify_service, "send_infra_toggle_telegram", lambda text, env: sent.append(text))
    monkeypatch.setattr(staging_service, "_push_config", lambda db: None)

    def fake(p, a, r, email, cfg):
        calls.append((a, r))
        if unchanged:
            return git_service.ReplicasToggleResult(ok=False, message="File x không thay đổi (giá trị hiện tại đã đúng)", changed=((p, a),))
        return git_service.ReplicasToggleResult(ok=True, message="Đã cập nhật", changed=((p, a),))
    monkeypatch.setattr(git_service, "toggle_staging_app", fake)
    return sent


def _mk_toggle_sched(db_session, user, action, now):
    s = AppSchedule(action=action, env="staging", project="grp", application="svc", frequency="daily", time_of_day="10:00",
                    next_run_at=now, is_active=True, created_by_id=user.id, created_by_email=user.email)
    db_session.add(s)
    db_session.commit()
    return s


def test_stop_then_start_restores_previous_replicas(make_user_factory, db_session, monkeypatch):
    from app.models import AppReplicasRestore
    user = make_user_factory(role=Role.SUPER_ADMIN, email="sa@example.com")
    db_session.add(EnvAppCache(env="staging", project="grp", application="svc", replicas="3"))
    db_session.commit()
    calls = []
    sent = _toggle_env(monkeypatch, db_session, calls)
    now = datetime(2026, 10, 16, 3, 0)

    stop = _mk_toggle_sched(db_session, user, "stop", now)
    schedule_service.run_due_schedules(db_session, now=now)
    db_session.refresh(stop)
    assert calls == [("svc", 0)] and stop.last_ok and not stop.last_skipped
    assert db_session.query(AppReplicasRestore).one().replicas == 3
    assert db_session.query(EnvAppCache).one().replicas == "0"

    start = _mk_toggle_sched(db_session, user, "start", now)
    schedule_service.run_due_schedules(db_session, now=now)
    db_session.refresh(start)
    assert calls[-1] == ("svc", 3) and "khôi phục 3 pod" in start.last_message
    assert db_session.query(AppReplicasRestore).count() == 0
    assert db_session.query(EnvAppCache).one().replicas == "3"
    assert len(sent) == 2


def test_skip_when_app_already_in_target_state(make_user_factory, db_session, monkeypatch):
    user = make_user_factory(role=Role.SUPER_ADMIN, email="sa@example.com")
    cache = EnvAppCache(env="staging", project="grp", application="svc", replicas="2")
    db_session.add(cache)
    db_session.commit()
    calls = []
    sent = _toggle_env(monkeypatch, db_session, calls)
    now = datetime(2026, 10, 16, 3, 0)

    start = _mk_toggle_sched(db_session, user, "start", now)  # dang chay 2 pod -> bo qua, giu 2 pod
    schedule_service.run_due_schedules(db_session, now=now)
    db_session.refresh(start)
    assert start.last_skipped and start.last_ok and "2 pod" in start.last_message

    cache.replicas = "0"
    db_session.commit()
    stop = _mk_toggle_sched(db_session, user, "stop", now)  # da tat -> bo qua
    schedule_service.run_due_schedules(db_session, now=now)
    db_session.refresh(stop)
    assert stop.last_skipped and stop.last_ok
    assert calls == [] and sent == []


def test_skip_when_git_reports_unchanged(make_user_factory, db_session, monkeypatch):
    user = make_user_factory(role=Role.SUPER_ADMIN, email="sa@example.com")
    db_session.add(EnvAppCache(env="staging", project="grp", application="svc", replicas="1"))  # cache cu: tuong dang chay
    db_session.commit()
    calls = []
    sent = _toggle_env(monkeypatch, db_session, calls, unchanged=True)
    now = datetime(2026, 10, 16, 3, 0)

    stop = _mk_toggle_sched(db_session, user, "stop", now)
    schedule_service.run_due_schedules(db_session, now=now)
    db_session.refresh(stop)
    assert calls == [("svc", 0)] and stop.last_skipped and stop.last_ok and sent == []


def test_bulk_edit_keeps_env_and_app(client_factory, make_user_factory, db_session):
    user = make_user_factory(email="s@example.com", can_schedule_toggle_staging=True)
    for project, app, env in [("pay", "api", "staging"), ("pay", "worker", "staging"), ("pay", "api", "production")]:
        db_session.add(EnvAppCache(env=env, project=project, application=app, replicas="1"))
    db_session.commit()
    now = datetime.utcnow()
    rows = []
    for app, env in [("api", "staging"), ("worker", "staging"), ("api", "production")]:
        s = AppSchedule(action="stop", env=env, project="pay", application=app, frequency="daily", time_of_day="22:00",
                        next_run_at=now, is_active=False, created_by_id=user.id, created_by_email=user.email)
        db_session.add(s)
        rows.append(s)
    db_session.commit()
    client = client_factory(user)

    client.post("/schedule/bulk-edit", data={
        "ids": [s.id for s in rows], "action": "start", "env": "staging", "frequency": "weekly",
        "time_of_day": "06:30", "weekday": "0",
    }, follow_redirects=False)
    for s in rows:
        db_session.refresh(s)
    a, w, prod = rows
    assert [(x.env, x.application, x.action, x.frequency, x.weekday, x.time_of_day, x.is_active) for x in (a, w)] == [
        ("staging", "api", "start", "weekly", 0, "06:30", True),
        ("staging", "worker", "start", "weekly", 0, "06:30", True),
    ]
    # Khong co quyen Schedule Tat/Bat Production -> giu nguyen.
    assert (prod.action, prod.time_of_day, prod.is_active) == ("stop", "22:00", False)


def test_default_order_and_finished_once(client_factory, make_user_factory, db_session):
    user = make_user_factory(role=Role.SUPER_ADMIN, email="sa@example.com")
    now = datetime.utcnow()
    for env, project, app, active in [("staging", "b", "x", True), ("production", "z", "y", False),
                                      ("staging", "a", "z", False), ("production", "a", "q", True)]:
        db_session.add(AppSchedule(action="stop", env=env, project=project, application=app, frequency="daily",
                                   time_of_day="22:00", next_run_at=now, is_active=active,
                                   created_by_id=user.id, created_by_email=user.email))
    done = AppSchedule(action="stop", env="staging", project="c", application="once", frequency="once",
                       run_once_at=datetime(2020, 1, 1), next_run_at=None, is_active=False,
                       created_by_id=user.id, created_by_email=user.email)
    db_session.add(done)
    db_session.commit()
    client = client_factory(user)

    order = [(r["id"], r["finished"]) for r in client.get("/schedule/status?tab=toggle").json()]
    rows = {s.id: s for s in db_session.query(AppSchedule).all()}
    assert [(rows[i].env, rows[i].project, rows[i].application) for i, _ in order] == [
        ("production", "a", "q"), ("production", "z", "y"), ("staging", "a", "z"), ("staging", "b", "x"), ("staging", "c", "once"),
    ]
    assert dict(order)[done.id] is True
    assert "Đã chạy xong" in client.get("/schedule?tab=toggle").text

    r = client.post(f"/schedule/{done.id}/active", data={"active": "1", "tab": "toggle"}, headers={"Accept": "application/json"})
    assert r.json() == {"ok": False, "message": "Lịch 1 lần đã chạy xong/qua giờ, không thể bật lại - hãy sửa ngày giờ mới"}
    resp = client.post("/schedule/bulk", data={"op": "resume", "tab": "toggle", "ids": [done.id]}, headers={"Accept": "application/json"}, follow_redirects=False)
    assert resp.status_code == 303


def test_schedule_view_permission_is_read_only(client_factory, make_user_factory, db_session):
    owner = make_user_factory(role=Role.SUPER_ADMIN, email="sa@example.com")
    db_session.add(EnvAppCache(env="staging", project="grp", application="svc", replicas="1"))
    stg = AppSchedule(action="stop", env="staging", project="grp", application="svc", frequency="daily", time_of_day="22:00",
                      next_run_at=datetime.utcnow(), is_active=True, created_by_id=owner.id, created_by_email=owner.email)
    prod = AppSchedule(action="stop", env="production", project="grp", application="svc", frequency="daily", time_of_day="22:00",
                       next_run_at=datetime.utcnow(), is_active=True, created_by_id=owner.id, created_by_email=owner.email)
    db_session.add_all([stg, prod])
    db_session.commit()

    viewer = make_user_factory(email="v@example.com", can_schedule_toggle_view_staging=True)
    assert viewer.can_access_schedule and viewer.can_view_schedule("staging") and not viewer.can_view_schedule("production")
    assert viewer.can_view_schedule("staging", "stop") and not viewer.can_view_schedule("staging", "restart")
    client = client_factory(viewer)

    page = client.get("/schedule?tab=toggle")
    assert page.status_code == 200
    assert 'id="schedule-form"' not in page.text  # khong co form tao/sua
    assert 'class="schedule-edit-btn sch-row-edit"' not in page.text  # khong co nut sua/tam dung/xoa tren dong
    assert 'id="sch-bulk-form"' not in page.text and 'class="sch-select"' not in page.text  # khong co o tich/thanh hang loat
    assert "grp/svc" in page.text
    assert [r["id"] for r in client.get("/schedule/status?tab=toggle").json()] == [stg.id]

    client.post("/schedule/create", data={"action": "stop", "env": "staging", "apps": ["grp|svc"],
                                          "frequency": "daily", "time_of_day": "06:00"}, follow_redirects=False)
    client.post(f"/schedule/{stg.id}/active", data={"active": "0", "tab": "toggle"}, follow_redirects=False)
    client.post("/schedule/bulk", data={"op": "delete", "tab": "toggle", "ids": [stg.id]}, follow_redirects=False)
    db_session.refresh(stg)
    assert db_session.query(AppSchedule).count() == 2 and stg.is_active

    # Khong co quyen Schedule nao -> 403.
    assert client_factory(make_user_factory(email="n@example.com")).get("/schedule").status_code == 403


def test_schedule_permission_opens_only_its_tab(client_factory, make_user_factory, db_session):
    sa = make_user_factory(role=Role.SUPER_ADMIN, email="sa@example.com")
    for action in ("restart", "stop"):
        db_session.add(AppSchedule(action=action, env="staging", project="grp", application=f"{action}-app", frequency="daily",
                                   time_of_day="22:00", next_run_at=datetime.utcnow(), is_active=True,
                                   created_by_id=sa.id, created_by_email=sa.email))
    db_session.commit()

    def tabs(**perm):
        client = client_factory(make_user_factory(email=f"{list(perm)[0]}@example.com", **perm))
        return {tab: len(client.get(f"/schedule/status?tab={tab}").json()) for tab in ("restart", "toggle")}

    assert tabs(can_schedule_restart_staging=True) == {"restart": 1, "toggle": 0}
    assert tabs(can_schedule_toggle_staging=True) == {"restart": 0, "toggle": 1}
    assert tabs(can_schedule_restart_view_staging=True) == {"restart": 1, "toggle": 0}
    assert tabs(can_schedule_toggle_view_staging=True) == {"restart": 0, "toggle": 1}


def test_tabs_hidden_without_permission(client_factory, make_user_factory, db_session):
    client = client_factory(make_user_factory(email="t@example.com", can_schedule_toggle_staging=True))
    html = client.get("/schedule").text  # mac dinh tab restart -> khong co quyen -> chuyen sang tab tat/bat
    assert 'href="/schedule?tab=restart"' not in html
    assert 'href="/schedule?tab=toggle" class="active"' in html


def test_scheduled_toggle_retries_when_git_busy(make_user_factory, db_session, monkeypatch):
    from datetime import timedelta
    from app.services import git_service, notify_service, production_service
    user = make_user_factory(role=Role.SUPER_ADMIN, email="sa@example.com")
    db_session.add(EnvAppCache(env="production", project="web", application="docs", replicas="0"))
    db_session.commit()
    sent = []
    monkeypatch.setattr(notify_service, "send_infra_toggle_telegram", lambda text, env: sent.append(text))
    monkeypatch.setattr(production_service, "_push_config", lambda db: None)
    busy = {"on": True}

    def fake(p, a, r, e, c):
        if busy["on"]:
            raise git_service.GitLockBusyError("Hệ thống đang bận xử lý một thao tác Git khác trên 'master'")
        return git_service.ReplicasToggleResult(ok=True, message="Đã cập nhật", changed=((p, a),))
    monkeypatch.setattr(git_service, "toggle_production_app", fake)

    t0 = datetime(2026, 10, 18, 1, 0)
    s = AppSchedule(action="start", env="production", project="web", application="docs", frequency="once",
                    run_once_at=t0, next_run_at=t0, is_active=True, created_by_id=user.id, created_by_email=user.email)
    db_session.add(s)
    db_session.commit()

    schedule_service.run_due_schedules(db_session, now=t0)  # git ban -> giu lich, khong Telegram
    db_session.refresh(s)
    assert (s.is_active, s.next_run_at, s.last_run_at, sent) == (True, t0, None, [])

    busy["on"] = False
    schedule_service.run_due_schedules(db_session, now=t0 + timedelta(minutes=1))  # phut sau chay duoc
    db_session.refresh(s)
    assert s.last_ok is True and s.is_active is False and len(sent) == 1 and "[Scheduled] Turn on" in sent[0]


def test_scheduled_toggle_gives_up_after_grace(make_user_factory, db_session, monkeypatch):
    from datetime import timedelta
    from app.services import git_service, notify_service, staging_service
    user = make_user_factory(role=Role.SUPER_ADMIN, email="sa@example.com")
    db_session.add(EnvAppCache(env="staging", project="web", application="docs", replicas="1"))
    db_session.commit()
    sent = []
    monkeypatch.setattr(notify_service, "send_infra_toggle_telegram", lambda text, env: sent.append(text))
    monkeypatch.setattr(staging_service, "_push_config", lambda db: None)

    def always_busy(p, a, r, e, c):
        raise git_service.GitLockBusyError("busy")
    monkeypatch.setattr(git_service, "toggle_staging_app", always_busy)

    t0 = datetime(2026, 10, 18, 1, 0)
    s = AppSchedule(action="stop", env="staging", project="web", application="docs", frequency="daily", time_of_day="08:00",
                    next_run_at=t0, is_active=True, created_by_id=user.id, created_by_email=user.email)
    db_session.add(s)
    db_session.commit()
    for minute in range(0, 11):  # thu lai moi phut, phut thu 10 bo cuoc
        schedule_service.run_due_schedules(db_session, now=t0 + timedelta(minutes=minute))
    db_session.refresh(s)
    assert s.last_run_at is not None and s.last_ok is False and "Git bận" in s.last_message
    assert s.next_run_at == t0 + timedelta(days=1)
    assert len(sent) == 1 and "Git was busy" in sent[0]


def _group_env(monkeypatch, db_session, apps, busy=None):
    """Gia lap git set_production_replicas: ghi lai moi lan goi (1 lan = 1 commit)."""
    from app.services import git_service, notify_service, production_service
    calls, sent = [], []
    monkeypatch.setattr(notify_service, "send_infra_toggle_telegram", lambda text, env: sent.append(text))
    monkeypatch.setattr(production_service, "_push_config", lambda db: None)

    def fake_set(entries, email, cfg):
        if busy and busy["on"]:
            raise git_service.GitLockBusyError("busy")
        calls.append(list(entries))
        return git_service.ReplicasToggleResult(ok=True, message="Đã cập nhật", changed=tuple((p, a) for p, a, _ in entries))
    monkeypatch.setattr(git_service, "set_production_replicas", fake_set)
    for app, replicas in apps:
        db_session.add(EnvAppCache(env="production", project="web", application=app, replicas=replicas))
    db_session.commit()
    return calls, sent


def _group_schedules(db_session, user, action, apps, now):
    rows = []
    for app in apps:
        s = AppSchedule(action=action, env="production", project="web", application=app, frequency="daily", time_of_day="08:00",
                        next_run_at=now, is_active=True, created_by_id=user.id, created_by_email=user.email)
        db_session.add(s)
        rows.append(s)
    db_session.commit()
    return rows


def test_group_stop_then_start_one_commit_one_telegram(make_user_factory, db_session, monkeypatch):
    from datetime import timedelta
    user = make_user_factory(role=Role.SUPER_ADMIN, email="sa@example.com")
    calls, sent = _group_env(monkeypatch, db_session, [("a", "3"), ("b", "1"), ("c", "0")])
    t0 = datetime(2026, 10, 18, 1, 0)

    stops = _group_schedules(db_session, user, "stop", ["a", "b", "c"], t0)
    schedule_service.run_due_schedules(db_session, now=t0)
    assert calls == [[("web", "a", 0), ("web", "b", 0)]]  # 1 commit, c da tat san -> bo qua
    assert len(sent) == 1 and "(2 applications)" in sent[0] and "Skipped (already in target state):</b> 1" in sent[0]
    for s in stops:
        db_session.refresh(s)
    assert [(s.last_ok, s.last_skipped) for s in stops] == [(True, False), (True, False), (True, True)]

    starts = _group_schedules(db_session, user, "start", ["a", "b"], t0 + timedelta(hours=1))
    schedule_service.run_due_schedules(db_session, now=t0 + timedelta(hours=1))
    assert calls[-1] == [("web", "a", 3), ("web", "b", 1)]  # khoi phuc dung so pod tung app
    assert len(sent) == 2
    db_session.refresh(starts[0])
    assert "khôi phục 3 pod" in starts[0].last_message


def test_group_retries_together_when_git_busy(make_user_factory, db_session, monkeypatch):
    from datetime import timedelta
    user = make_user_factory(role=Role.SUPER_ADMIN, email="sa@example.com")
    busy = {"on": True}
    calls, sent = _group_env(monkeypatch, db_session, [("a", "1"), ("b", "1")], busy=busy)
    t0 = datetime(2026, 10, 18, 1, 0)
    rows = _group_schedules(db_session, user, "stop", ["a", "b"], t0)

    schedule_service.run_due_schedules(db_session, now=t0)
    for s in rows:
        db_session.refresh(s)
    assert [s.next_run_at for s in rows] == [t0, t0] and sent == []
    busy["on"] = False
    schedule_service.run_due_schedules(db_session, now=t0 + timedelta(minutes=1))
    assert calls == [[("web", "a", 0), ("web", "b", 0)]] and len(sent) == 1


def test_group_telegram_is_split_under_limit(make_user_factory, db_session, monkeypatch):
    user = make_user_factory(role=Role.SUPER_ADMIN, email="sa@example.com")
    apps = [f"very-long-application-name-{i:03d}" for i in range(300)]
    calls, sent = _group_env(monkeypatch, db_session, [(a, "1") for a in apps])
    t0 = datetime(2026, 10, 18, 1, 0)
    _group_schedules(db_session, user, "stop", apps, t0)

    schedule_service.run_due_schedules(db_session, now=t0)
    assert len(calls) == 1 and len(sent) > 1
    assert all(len(t) < 4096 for t in sent)
    assert f"(1/{len(sent)})" in sent[0] and f"({len(sent)}/{len(sent)})" in sent[-1]
    assert sum(t.count("very-long-application-name-") for t in sent) == 300
