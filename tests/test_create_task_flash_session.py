"""Kiểm thử luồng chính (happy path) sau khi flash message của POST /tasks/create
chuyển từ query string (`?ok=..&msg=..`) sang session (app/flash.py::set_flash/pop_flash) -
xem app/routers/tasks_router.py::create_submit.

Chỉ test 3 kịch bản chính theo yêu cầu:
1. Tạo task hợp lệ -> redirect 303 tới `/tasks/{id}` KHÔNG dính query string.
2. GET trang đích lần đầu vẫn thấy flash (qua session), GET lần 2 (F5) thì KHÔNG còn.
3. Nhánh lỗi giờ không hợp lệ -> 303 về `/tasks/create` sạch, flash lỗi vẫn tới template.
"""

import json

from app.models import Group, Project, Role
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


def test_create_submit_success_redirects_to_clean_task_url(client_factory, db_session, monkeypatch):
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
            "confirmed_run_at": "2026-08-01T10:00",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/tasks/")
    assert "?" not in location
    assert "ok=" not in location
    assert "msg=" not in location


def test_flash_shown_once_via_session_then_gone_on_second_get(client_factory, db_session, monkeypatch):
    user = make_user(db_session, role=Role.USER)
    group = make_group(db_session)
    make_project(db_session, group, application="api")
    _stub_commit_check_ok(monkeypatch)

    client = client_factory(user=user)
    create_resp = client.post(
        "/tasks/create",
        data={
            "group_id": group.id,
            "application": "api",
            "commitid": "abc1234",
            "config_env": "",
            "updatefor": "release",
            "confirmed_run_at": "2026-08-01T10:00",
        },
        follow_redirects=False,
    )
    target = create_resp.headers["location"]

    # base.html render flash qua `{{ flash_msg | tojson }}` trong <script> - Jinja tojson
    # mac dinh ensure_ascii=True nen chuoi UTF-8 bi escape thanh \uXXXX, khong con nguyen
    # van trong HTML; so sanh bang chinh json.dumps(...) cho dung nhung gi thuc su duoc
    # render (thay vi tim chuoi tieng Viet tho, se khong bao gio khop).
    expected_escaped_msg = json.dumps("Tạo yêu cầu thành công!")

    # Lan GET dau tien: flash phai duoc pop tu session va render duoc trong template.
    first_get = client.get(target)
    assert first_get.status_code == 200
    assert expected_escaped_msg in first_get.text
    assert "showToast(" in first_get.text

    # Lan GET thu 2 (F5): da bi pop o lan truoc, KHONG duoc hien lai.
    second_get = client.get(target)
    assert second_get.status_code == 200
    assert expected_escaped_msg not in second_get.text


def test_create_submit_invalid_run_at_redirects_clean_with_error_flash(client_factory, db_session, monkeypatch):
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
    location = resp.headers["location"]
    assert location == "/tasks/create"

    follow_get = client.get(location)
    assert follow_get.status_code == 200
    assert json.dumps("Giờ dự kiến không hợp lệ") in follow_get.text
