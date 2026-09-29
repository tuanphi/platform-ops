"""Test tập trung CSRF cho 11 route đổi trạng thái deploy trong actions_router.py.

Cơ chế đang test (xem app/auth.py::get_or_create_csrf_token/check_csrf_token +
app/routers/actions_router.py): mỗi route nhận `csrf_token: str = Form("")`, check là
DÒNG ĐẦU TIÊN trong handler (trước cả tra task/permission), sai/thiếu -> 403 qua
_csrf_fail_back/_csrf_fail_list, KHÔNG thực hiện hành động.

QUAN TRỌNG: dùng `client.post_without_csrf` (tham chiếu hàm post GỐC của TestClient, xem
tests/conftest.py::_wrap_client_auto_csrf) để tự kiểm soát chính xác giá trị csrf_token gửi
lên - KHÔNG dùng `client.post` thường (client.post tự động nhét token HỢP LỆ nếu thiếu, sẽ
làm sai lệch ý định của các test case CSRF thiếu/sai token ở đây).

File này CHỈ THÊM test - không sửa code app/."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.models import DeployTask, Role, TaskStatus
from tests.conftest import extract_csrf_token, make_user


def make_task(db_session, status=TaskStatus.TASK, **kwargs):
    defaults = dict(project="core", application="api", commitid="abc1234", status=status, email="owner@example.com")
    defaults.update(kwargs)
    task = DeployTask(**defaults)
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


def _valid_token(client):
    return extract_csrf_token(client.get("/tasks").text)


def _build_single_task(db_session, kind: str):
    """Tạo 1 task ở đúng trạng thái để hành động `kind` SẼ THÀNH CÔNG nếu không bị CSRF
    chặn - đảm bảo 403 quan sát được là do CSRF, không phải trùng hợp do sai trạng thái/
    thiếu quyền."""
    if kind == "cancel":
        return make_task(db_session, status=TaskStatus.TASK, commitid="csrfc001")
    if kind == "reject":
        return make_task(db_session, status=TaskStatus.TASK, commitid="csrfc002")
    if kind == "approve":
        return make_task(
            db_session, status=TaskStatus.TASK, commitid="csrfc003", confirmed_run_at=datetime.utcnow()
        )
    if kind == "run":
        return make_task(db_session, status=TaskStatus.APPROVED, commitid="csrfc004")
    if kind == "auto-deploy":
        return make_task(
            db_session, status=TaskStatus.APPROVED, commitid="csrfc005", confirmed_run_at=datetime.utcnow()
        )
    if kind == "rollback":
        return make_task(
            db_session, status=TaskStatus.DONE, commitid="csrfc006", image_version="v2", done_at=datetime.utcnow()
        )
    raise ValueError(kind)


SINGLE_ROUTE_KINDS = ["cancel", "reject", "approve", "run", "auto-deploy", "rollback"]
BULK_ROUTE_KINDS = ["cancel", "reject", "approve", "run", "delete"]


def _single_route_data(kind: str, db_session, task) -> dict:
    if kind == "auto-deploy":
        return {"enabled": "1", "auto_run_at": "2026-08-01T10:00"}
    if kind == "rollback":
        target = make_task(
            db_session, status=TaskStatus.DONE, commitid="csrfc999", image_version="v1"
        )
        return {"target_task_id": target.id}
    return {}


# ---------------------------------------------------------------------------
# 1. Thieu csrf_token -> 403, task KHONG doi trang thai (6 route don le)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", SINGLE_ROUTE_KINDS)
def test_single_route_missing_csrf_token_returns_403_and_no_state_change(client_factory, db_session, kind, monkeypatch):
    from app.services import task_service

    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: task_service.git_service.RunResult(ok=True, message="ok", image_version="v1"),
    )
    monkeypatch.setattr(
        task_service.git_service,
        "perform_rollback",
        lambda *a, **k: task_service.git_service.RunResult(ok=True, message="ok", image_version="v1"),
    )

    admin = make_user(db_session, email=f"csrf-missing-{kind}@example.com", role=Role.ADMIN)
    task = _build_single_task(db_session, kind)
    original_status = task.status

    client = client_factory(user=admin)
    data = _single_route_data(kind, db_session, task)
    # KHONG dua "csrf_token" vao data -> mo phong request hoan toan khong gui truong nay.
    resp = client.post_without_csrf(f"/tasks/{task.id}/{kind}", data=data, headers={"Accept": "application/json"})

    assert resp.status_code == 403, f"[{kind}] ky vong 403 khi thieu csrf_token"
    body = resp.json()
    assert body["ok"] is False
    assert "quyền" not in body["message"], f"[{kind}] loi CSRF khong duoc lo message nghiep vu binh thuong"
    db_session.refresh(task)
    assert task.status == original_status, f"[{kind}] task KHONG duoc doi trang thai khi CSRF that bai"


@pytest.mark.parametrize("kind", SINGLE_ROUTE_KINDS)
def test_single_route_wrong_csrf_token_returns_403_and_no_state_change(client_factory, db_session, kind, monkeypatch):
    from app.services import task_service

    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: task_service.git_service.RunResult(ok=True, message="ok", image_version="v1"),
    )
    monkeypatch.setattr(
        task_service.git_service,
        "perform_rollback",
        lambda *a, **k: task_service.git_service.RunResult(ok=True, message="ok", image_version="v1"),
    )

    admin = make_user(db_session, email=f"csrf-wrong-{kind}@example.com", role=Role.ADMIN)
    task = _build_single_task(db_session, kind)
    original_status = task.status

    client = client_factory(user=admin)
    data = _single_route_data(kind, db_session, task)
    data["csrf_token"] = "token-gia-mao-khong-khop-voi-session"
    resp = client.post_without_csrf(f"/tasks/{task.id}/{kind}", data=data, headers={"Accept": "application/json"})

    assert resp.status_code == 403, f"[{kind}] ky vong 403 khi csrf_token sai"
    assert resp.json()["ok"] is False
    db_session.refresh(task)
    assert task.status == original_status, f"[{kind}] task KHONG duoc doi trang thai khi CSRF sai"


@pytest.mark.parametrize("kind", SINGLE_ROUTE_KINDS)
def test_single_route_valid_csrf_token_allows_action(client_factory, db_session, kind, monkeypatch):
    """Ngược lại - token HỢP LỆ (lấy đúng từ session của chính client) phải cho hành động
    đi tiếp bình thường (không còn bị chặn ở bước CSRF)."""
    from app.services import task_service

    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: task_service.git_service.RunResult(ok=True, message="ok", image_version="v1"),
    )
    monkeypatch.setattr(
        task_service.git_service,
        "perform_rollback",
        lambda *a, **k: task_service.git_service.RunResult(ok=True, message="ok", image_version="v1"),
    )

    admin = make_user(db_session, email=f"csrf-valid-{kind}@example.com", role=Role.ADMIN)
    task = _build_single_task(db_session, kind)

    client = client_factory(user=admin)
    data = _single_route_data(kind, db_session, task)
    # client.post (khong phai post_without_csrf) tu dong nhet token HOP LE that su.
    resp = client.post(f"/tasks/{task.id}/{kind}", data=data, headers={"Accept": "application/json"})

    assert resp.status_code == 200, f"[{kind}] token hop le phai duoc chap nhan, thuc te {resp.status_code}: {resp.text}"
    assert resp.json()["ok"] is True, f"[{kind}] hanh dong phai thanh cong voi token hop le: {resp.json()}"


# ---------------------------------------------------------------------------
# 2. Bulk routes (5 route) - thieu/sai token -> 403, khong task nao bi doi trang thai
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", BULK_ROUTE_KINDS)
def test_bulk_route_missing_csrf_token_returns_403_and_no_state_change(client_factory, db_session, kind):
    if kind == "delete":
        admin = make_user(db_session, email=f"csrf-bulk-missing-{kind}@example.com", role=Role.SUPER_ADMIN)
        task = make_task(db_session, status=TaskStatus.DONE, commitid="csrfb001", done_at=datetime.utcnow())
    else:
        admin = make_user(db_session, email=f"csrf-bulk-missing-{kind}@example.com", role=Role.ADMIN)
        task = make_task(db_session, status=TaskStatus.TASK, commitid="csrfb002")
    original_status = task.status

    client = client_factory(user=admin)
    resp = client.post_without_csrf(
        f"/tasks/bulk/{kind}", data={"task_ids": [task.id]}, headers={"Accept": "application/json"}
    )

    assert resp.status_code == 403, f"[bulk/{kind}] ky vong 403 khi thieu csrf_token"
    assert resp.json()["ok"] is False
    still_exists = db_session.get(DeployTask, task.id)
    assert still_exists is not None, f"[bulk/{kind}] task khong duoc XOA khi CSRF that bai"
    db_session.refresh(still_exists)
    assert still_exists.status == original_status, f"[bulk/{kind}] task KHONG doi trang thai khi CSRF that bai"


@pytest.mark.parametrize("kind", BULK_ROUTE_KINDS)
def test_bulk_route_wrong_csrf_token_returns_403_and_no_state_change(client_factory, db_session, kind):
    if kind == "delete":
        admin = make_user(db_session, email=f"csrf-bulk-wrong-{kind}@example.com", role=Role.SUPER_ADMIN)
        task = make_task(db_session, status=TaskStatus.DONE, commitid="csrfb003", done_at=datetime.utcnow())
    else:
        admin = make_user(db_session, email=f"csrf-bulk-wrong-{kind}@example.com", role=Role.ADMIN)
        task = make_task(db_session, status=TaskStatus.TASK, commitid="csrfb004")
    original_status = task.status

    client = client_factory(user=admin)
    resp = client.post_without_csrf(
        f"/tasks/bulk/{kind}",
        data={"task_ids": [task.id], "csrf_token": "token-gia-mao"},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 403, f"[bulk/{kind}] ky vong 403 khi csrf_token sai"
    assert resp.json()["ok"] is False
    still_exists = db_session.get(DeployTask, task.id)
    assert still_exists is not None
    db_session.refresh(still_exists)
    assert still_exists.status == original_status


@pytest.mark.parametrize("kind", BULK_ROUTE_KINDS)
def test_bulk_route_valid_csrf_token_allows_action(client_factory, db_session, kind):
    if kind == "delete":
        admin = make_user(db_session, email=f"csrf-bulk-valid-{kind}@example.com", role=Role.SUPER_ADMIN)
        task = make_task(db_session, status=TaskStatus.DONE, commitid="csrfb005", done_at=datetime.utcnow())
    else:
        admin = make_user(db_session, email=f"csrf-bulk-valid-{kind}@example.com", role=Role.ADMIN)
        task = make_task(db_session, status=TaskStatus.TASK, commitid="csrfb006")

    client = client_factory(user=admin)
    resp = client.post(f"/tasks/bulk/{kind}", data={"task_ids": [task.id]}, headers={"Accept": "application/json"})

    assert resp.status_code == 200, f"[bulk/{kind}] token hop le phai duoc chap nhan: {resp.text}"
    assert resp.json()["ok"] is True, f"[bulk/{kind}]: {resp.json()}"


# ---------------------------------------------------------------------------
# 3. Token cua session (user) KHAC khong dung chung duoc
# ---------------------------------------------------------------------------


def test_csrf_token_from_a_different_session_is_rejected(client_factory, db_session):
    """Token hop le CUA CLIENT/SESSION A khong duoc chap nhan khi gui boi CLIENT/SESSION B -
    moi session co token CSRF rieng, khong dung chung duoc (chan duoc kich ban ke tan cong
    biet truoc 1 token 'hop le' bat ky roi phat tan/tai su dung o session khac)."""
    admin_a = make_user(db_session, email="csrf-session-a@example.com", role=Role.ADMIN)
    admin_b = make_user(db_session, email="csrf-session-b@example.com", role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.TASK, commitid="csrfsess1")

    client_a = client_factory(user=admin_a)
    client_b = client_factory(user=admin_b)

    token_from_a = _valid_token(client_a)

    resp = client_b.post_without_csrf(
        f"/tasks/{task.id}/cancel",
        data={"csrf_token": token_from_a},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 403, "Token cua session A khong duoc chap nhan boi session B"
    assert resp.json()["ok"] is False
    db_session.refresh(task)
    assert task.status == TaskStatus.TASK


# ---------------------------------------------------------------------------
# 4. Nhanh AJAX tra JSON {ok:false,message}, nhanh form thuong redirect + flash, URL SACH
# ---------------------------------------------------------------------------


def test_csrf_failure_ajax_branch_returns_clean_json_not_raw_error(client_factory, db_session):
    admin = make_user(db_session, email="csrf-ajax-branch@example.com", role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.TASK, commitid="csrfajax1")

    client = client_factory(user=admin)
    resp = client.post_without_csrf(
        f"/tasks/{task.id}/cancel", data={}, headers={"Accept": "application/json"}
    )

    assert resp.status_code == 403
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert set(body.keys()) == {"ok", "message"}
    assert body["ok"] is False
    assert isinstance(body["message"], str) and body["message"]


def test_csrf_failure_form_branch_redirects_with_clean_url_and_session_flash(client_factory, db_session):
    admin = make_user(db_session, email="csrf-form-branch@example.com", role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.TASK, commitid="csrfform1")

    client = client_factory(user=admin)
    resp = client.post_without_csrf(f"/tasks/{task.id}/cancel", data={}, follow_redirects=False)

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location == f"/tasks/{task.id}"
    assert "ok=" not in location
    assert "msg=" not in location

    follow = client.get(location)
    assert follow.status_code == 200
    import json

    from app.routers.actions_router import _CSRF_ERROR_MESSAGE

    # flash_msg render qua Jinja `| tojson` (base.html), khong phai chuoi tho - so sanh
    # bang json.dumps() giong quy uoc da dung o test_run_rollback_rate_limit.py.
    assert json.dumps(_CSRF_ERROR_MESSAGE) in follow.text

    db_session.refresh(task)
    assert task.status == TaskStatus.TASK


def test_csrf_failure_bulk_form_branch_redirects_to_tasks_list_clean_url(client_factory, db_session):
    """Bulk dùng _csrf_fail_list -> redirect về /tasks (không phải /tasks/{id}) khi không
    có next hợp lệ, vẫn phải SẠCH URL, và không task nào bị Reject."""
    admin = make_user(db_session, email="csrf-bulk-form-branch@example.com", role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.TASK, commitid="csrfbform1")

    client = client_factory(user=admin)
    resp = client.post_without_csrf(
        "/tasks/bulk/reject", data={"task_ids": [task.id]}, follow_redirects=False
    )

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location == "/tasks"
    assert "ok=" not in location
    assert "msg=" not in location

    db_session.refresh(task)
    assert task.status == TaskStatus.TASK
