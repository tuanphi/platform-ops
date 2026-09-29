"""Kiểm thử POST /tasks/create - từ khi field `confirmed_run_at` (giờ nhắc nhở, KHÔNG
phải Auto deploy) được chuyển từ bước Approve sang bước Create (bắt buộc, kiểu
datetime-local GMT+7, convert sang UTC trước khi lưu DB - xem
app/routers/tasks_router.py::create_submit và app/timeutil.py::gmt7_to_utc)."""

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


def _stub_commit_check_ok(monkeypatch):
    monkeypatch.setattr(
        task_service.git_service,
        "check_commit_on_staging",
        lambda *a, **k: task_service.git_service.CommitCheckResult(status="ok", message="ok", image_version="v1"),
    )


# ---------------------------------------------------------------------------
# confirmed_run_at bắt buộc (Form(...)) - thiếu field bị FastAPI chặn 422
# ---------------------------------------------------------------------------


def test_create_submit_missing_confirmed_run_at_returns_422(client_factory, db_session):
    user = make_user(db_session, role=Role.USER)
    group = make_group(db_session)
    make_project(db_session, group, application="api")

    client = client_factory(user=user)
    resp = client.post(
        "/tasks/create",
        data={
            "group_id": group.id,
            "application": "api",
            "commitid": "abc1234",
            "config_env": "",
            "updatefor": "release",
            # confirmed_run_at bi thieu co y
        },
    )

    assert resp.status_code == 422
    assert db_session.query(DeployTask).count() == 0


# ---------------------------------------------------------------------------
# Convert GMT+7 -> UTC đúng khi lưu
# ---------------------------------------------------------------------------


def test_create_submit_valid_confirmed_run_at_converts_gmt7_to_utc(client_factory, db_session, monkeypatch):
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
            "confirmed_run_at": "2026-08-01T10:00",  # gio nhap la GMT+7
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/tasks/")

    task = db_session.query(DeployTask).one()
    assert task.status == TaskStatus.TASK
    # GMT7_OFFSET = 7h -> UTC = local - 7h
    assert task.confirmed_run_at == datetime(2026, 8, 1, 3, 0, 0)


# ---------------------------------------------------------------------------
# Giờ nhập sai định dạng -> redirect ok=0, KHÔNG tạo task, KHÔNG crash 500
# ---------------------------------------------------------------------------


def test_create_submit_confirmed_run_at_garbage_string_redirects_without_creating_task(
    client_factory, db_session, monkeypatch
):
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
            "confirmed_run_at": "not-a-date",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    # Flash loi nay chuyen sang session (app/flash.py::set_flash), redirect target phai
    # SACH, khong con dinh ?ok=0&msg=... nhu truoc.
    assert resp.headers["location"] == "/tasks/create"
    assert db_session.query(DeployTask).count() == 0


def test_create_submit_confirmed_run_at_out_of_range_value_redirects_without_creating_task(
    client_factory, db_session, monkeypatch
):
    """VD kiểu tháng=13/ngày=40 - datetime.fromisoformat ném ValueError, phải được bắt
    và redirect, không được để lộ traceback/500."""
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
            "confirmed_run_at": "2026-13-40T10:00",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/tasks/create"
    assert db_session.query(DeployTask).count() == 0


def test_create_submit_confirmed_run_at_empty_string_redirects_without_creating_task(
    client_factory, db_session, monkeypatch
):
    """Form(...) chỉ bảo đảm field CÓ MẶT, chuỗi rỗng vẫn qua được lớp validation của
    FastAPI - phải bị chặn ở tầng parse datetime trong create_submit, không phải 422
    cũng không phải 500."""
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
            "confirmed_run_at": "",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/tasks/create"
    assert db_session.query(DeployTask).count() == 0
