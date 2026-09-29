"""Kiểm thử luồng chính (happy path) Phase 2+3: sau khi 6 helper redirect chuyển từ
`?ok=..&msg=..` sang session (app/flash.py) + template task_detail.html thêm class
`ajax-form` cho 4 form POST - URL sau redirect phải SẠCH (không còn `?ok=`/`msg=`) mà
flash vẫn hiển thị được qua session; nhánh AJAX (header Accept: application/json) vẫn
trả JSON, không redirect.

Chỉ test 4 kịch bản chính theo yêu cầu, không đào sâu edge case."""

import json

from app.models import Role, TaskStatus
from tests.conftest import make_user
from tests.test_run_rollback_rate_limit import make_task


def test_cancel_normal_form_redirects_clean_and_flash_reaches_template(client_factory, db_session):
    user = make_user(db_session, email="owner@example.com", role=Role.USER)
    task = make_task(db_session, status=TaskStatus.TASK, email="owner@example.com")

    client = client_factory(user=user)
    resp = client.post(f"/tasks/{task.id}/cancel", data={}, follow_redirects=False)

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location == f"/tasks/{task.id}"
    assert "ok=" not in location
    assert "msg=" not in location

    get_resp = client.get(location)
    assert get_resp.status_code == 200
    assert json.dumps("Task cancelled!") in get_resp.text


def test_cancel_ajax_request_returns_json_not_redirect(client_factory, db_session):
    user = make_user(db_session, email="owner2@example.com", role=Role.USER)
    task = make_task(db_session, status=TaskStatus.TASK, email="owner2@example.com")

    client = client_factory(user=user)
    resp = client.post(
        f"/tasks/{task.id}/cancel",
        data={},
        headers={"Accept": "application/json"},
        follow_redirects=False,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["message"] == "Task cancelled!"


def test_users_role_change_redirects_clean_to_users_list(client_factory, db_session):
    admin = make_user(db_session, email="super@example.com", role=Role.SUPER_ADMIN)
    target = make_user(db_session, email="target@example.com", role=Role.USER)

    client = client_factory(user=admin)
    resp = client.post(f"/users/{target.id}/role", data={"role": "Admin"}, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/users"


def test_next_url_with_existing_querystring_is_preserved_unchanged(client_factory, db_session):
    admin = make_user(db_session, email="rl-next@example.com", role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.TASK, commitid="nxt0001")

    client = client_factory(user=admin)
    next_url = "/tasks?status=TASK&page=2"
    resp = client.post(f"/tasks/{task.id}/reject", data={"next": next_url}, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == next_url
