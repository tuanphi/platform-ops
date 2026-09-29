"""Kiểm thử server-side parse datetime sau khi input đổi từ type="datetime-local" sang
type="text" dd/mm/yyyy hh:mm (validate + convert sang ISO yyyy-MM-ddThh:mm nằm ở JS
parseDmyDatetime trong base.html - xem tests/test_dmy_js_datetime_parse_format.py). Backend
KHÔNG đổi: vẫn nhận chuỗi ISO qua Form field rồi `datetime.fromisoformat(...)`. Test này mô
phỏng client BYPASS JS validate (gửi thẳng chuỗi tuỳ ý qua HTTP, như JS bị tắt/bug/tool như
curl) - server phải tự chống chịu được, không bao giờ 500 unhandled.

2 endpoint liên quan:
  - POST /tasks/create   (field confirmed_run_at) - app/routers/tasks_router.py::create_submit
  - POST /tasks/{id}/auto-deploy (field auto_run_at) - app/routers/actions_router.py::auto_deploy
"""

from datetime import datetime

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
    project = Project(group_id=group.id, application_name=application, is_active=is_active)
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)
    return project


def make_task(db_session, status=TaskStatus.TASK, **kwargs):
    defaults = dict(project="core", application="api", commitid="abc1234", status=status, email="owner@example.com")
    defaults.update(kwargs)
    task = DeployTask(**defaults)
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


def _stub_commit_check_ok(monkeypatch):
    monkeypatch.setattr(
        task_service.git_service,
        "check_commit_on_staging",
        lambda *a, **k: task_service.git_service.CommitCheckResult(status="ok", message="ok", image_version="v1"),
    )


# ---------------------------------------------------------------------------
# POST /tasks/{id}/auto-deploy - cac gia tri auto_run_at bat thuong
# ---------------------------------------------------------------------------


def test_auto_deploy_valid_iso_string_creates_expected_utc_value(client_factory, db_session):
    admin = make_user(db_session, role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.APPROVED, confirmed_run_at=datetime.utcnow())

    client = client_factory(user=admin)
    resp = client.post(
        f"/tasks/{task.id}/auto-deploy",
        data={"enabled": "1", "auto_run_at": "2026-08-01T10:00"},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    db_session.refresh(task)
    assert task.auto_run_at == datetime(2026, 8, 1, 3, 0, 0)


def test_auto_deploy_garbage_string_rejected_not_500(client_factory, db_session):
    admin = make_user(db_session, role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.APPROVED, confirmed_run_at=datetime.utcnow())

    client = client_factory(user=admin)
    resp = client.post(
        f"/tasks/{task.id}/auto-deploy",
        data={"enabled": "1", "auto_run_at": "not-a-date"},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    db_session.refresh(task)
    assert task.auto_deploy is False
    assert task.auto_run_at is None


def test_auto_deploy_out_of_range_month_day_rejected_not_500(client_factory, db_session):
    """31/02 -> ISO gia (2026-13-40T10:00 kieu) - datetime.fromisoformat nem ValueError,
    phai duoc bat, khong duoc de lo 500/traceback."""
    admin = make_user(db_session, role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.APPROVED, confirmed_run_at=datetime.utcnow())

    client = client_factory(user=admin)
    resp = client.post(
        f"/tasks/{task.id}/auto-deploy",
        data={"enabled": "1", "auto_run_at": "2026-13-40T10:00"},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    db_session.refresh(task)
    assert task.auto_deploy is False


def test_auto_deploy_empty_string_when_enabling_rejected_not_500(client_factory, db_session):
    admin = make_user(db_session, role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.APPROVED, confirmed_run_at=datetime.utcnow())

    client = client_factory(user=admin)
    resp = client.post(
        f"/tasks/{task.id}/auto-deploy",
        data={"enabled": "1", "auto_run_at": ""},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    db_session.refresh(task)
    assert task.auto_deploy is False


def test_auto_deploy_extreme_early_value_triggers_overflow_in_gmt7_to_utc(client_factory, db_session):
    """BUG CANDIDATE: gmt7_to_utc() = local_dt - 7h. Voi auto_run_at rat gan datetime.min
    (vd '0001-01-01T02:00'), phep tru nay tran ra ngoai pham vi bieu dien duoc cua Python
    datetime -> OverflowError. app/routers/tasks_router.py::create_submit bat ca
    `(ValueError, OverflowError)`, NHUNG app/routers/actions_router.py::auto_deploy (dong
    ~313) chi bat `except ValueError:` - KHONG bat OverflowError. Neu day thuc su la unhandled
    exception (khong phai 200 ok=False) thi day la 1 bug logic (thieu doi xung voi
    create_submit) can bao ve [dev]; KHONG phai van de bao mat (khong lo secret, khong bypass
    quyen) nhung co the gay 500/DoS don gian tren 1 request neu tool/curl gui thang gia tri
    nay (JS client hop le se khong bao gio gui vi input dd/mm/yyyy nam 0001 la phi thuc te
    nhung khong bi JS chan cung)."""
    admin = make_user(db_session, role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.APPROVED, confirmed_run_at=datetime.utcnow())

    client = client_factory(user=admin)
    resp = client.post(
        f"/tasks/{task.id}/auto-deploy",
        data={"enabled": "1", "auto_run_at": "0001-01-01T02:00"},
        headers={"Accept": "application/json"},
    )

    # Ky vong: server phai tu chong chiu (200 + ok=False), GIONG create_submit.
    assert resp.status_code == 200, (
        "auto_deploy khong bat OverflowError tu gmt7_to_utc() nhu create_submit da lam -> "
        f"co the la unhandled 500. status={resp.status_code}"
    )
    body = resp.json()
    assert body["ok"] is False
    db_session.refresh(task)
    assert task.auto_deploy is False


def test_auto_deploy_disable_does_not_require_valid_run_at(client_factory, db_session):
    """Tat Auto (enabled=0) khong bat buoc auto_run_at hop le - gia tri rac o day phai bi
    bo qua hoan toan (khong parse), khac voi truong hop bat (enabled=1)."""
    admin = make_user(db_session, role=Role.ADMIN)
    task = make_task(
        db_session,
        status=TaskStatus.APPROVED,
        confirmed_run_at=datetime.utcnow(),
        auto_deploy=True,
        auto_run_at=datetime.utcnow(),
    )

    client = client_factory(user=admin)
    resp = client.post(
        f"/tasks/{task.id}/auto-deploy",
        data={"enabled": "0", "auto_run_at": "garbage-value"},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    db_session.refresh(task)
    assert task.auto_deploy is False
    assert task.auto_run_at is None


# ---------------------------------------------------------------------------
# POST /tasks/create - confirmed_run_at (da co test rieng o test_create_task_flow.py cho
# missing/valid/garbage/out-of-range/empty). Bo sung case OverflowError that su (da duoc
# bat trong create_submit) de xac nhan hanh vi dung, va whitespace/None-like string.
# ---------------------------------------------------------------------------


def test_create_submit_extreme_early_value_overflow_handled_gracefully(client_factory, db_session, monkeypatch):
    user = make_user(db_session, role=Role.USER)
    group = make_group(db_session)
    make_project(db_session, group, application="api")
    _stub_commit_check_ok(monkeypatch)

    client = client_factory(user=user)
    resp = client.post(
        "/tasks/create",
        data={
            "group_id": group.id,
            "application": "api",
            "commitid": "abc1234",
            "config_env": "",
            "updatefor": "release",
            "confirmed_run_at": "0001-01-01T02:00",  # gmt7_to_utc se tru 7h -> tran datetime.min
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    # Flash loi nay chuyen sang session (app/flash.py::set_flash), redirect target phai
    # SACH, khong con dinh ?ok=0&msg=... nhu truoc.
    assert resp.headers["location"] == "/tasks/create"
    assert db_session.query(DeployTask).count() == 0


def test_create_submit_whitespace_only_confirmed_run_at_rejected_not_500(client_factory, db_session, monkeypatch):
    user = make_user(db_session, role=Role.USER)
    group = make_group(db_session)
    make_project(db_session, group, application="api")
    _stub_commit_check_ok(monkeypatch)

    client = client_factory(user=user)
    resp = client.post(
        "/tasks/create",
        data={
            "group_id": group.id,
            "application": "api",
            "commitid": "abc1234",
            "config_env": "",
            "updatefor": "release",
            "confirmed_run_at": "   ",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/tasks/create"
    assert db_session.query(DeployTask).count() == 0
