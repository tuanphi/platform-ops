"""Kiểm thử HAPPY PATH cho thay đổi UI: '/' redirect sang '/home' (Home), nút
"Tạo request" và click-through status card trên dashboard.html/base.html.

Theo yêu cầu tester: chỉ 1-3 test case luồng chính, dùng chung client_factory/make_user
từ tests/conftest.py (KHÔNG chạm DB/mạng thật) - xem tests/test_router_integration.py.
"""

from app.models import Role
from tests.conftest import make_user


def test_index_redirects_to_home_and_renders_home(client_factory, db_session):
    """TC1 (AC1): GET '/' -> 303 tới '/home'; GET '/home' -> 200, chứa 'Home'."""
    user = make_user(db_session, role=Role.USER)
    client = client_factory(user=user)

    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/home"

    resp2 = client.get("/home")
    assert resp2.status_code == 200
    assert "Home" in resp2.text


def test_dashboard_has_create_request_link(client_factory, db_session):
    """TC2 (AC2): '/home' chứa link 'Tạo request' trỏ '/tasks/create'."""
    user = make_user(db_session, role=Role.USER)
    client = client_factory(user=user)

    resp = client.get("/home")
    assert resp.status_code == 200
    assert 'href="/tasks/create"' in resp.text
    assert "Tạo request" in resp.text


def test_dashboard_has_danh_sach_button_linking_to_tasks(client_factory, db_session):
    """TC2b (AC2): '/home' chứa nút nổi bật 'Danh sách' trỏ '/tasks' (thay cho line chart
    'Phân bố trạng thái' đã bị bỏ)."""
    user = make_user(db_session, role=Role.USER)
    client = client_factory(user=user)

    resp = client.get("/home")
    assert resp.status_code == 200
    assert 'href="/tasks"' in resp.text
    assert "Danh sách" in resp.text
    assert "Phân bố trạng thái" not in resp.text


def test_dashboard_status_cards_link_to_filtered_tasks(client_factory, db_session):
    """TC3 (AC3): mỗi thẻ trạng thái trên '/home' là link '/tasks?status=<status>'
    (kiểm Running, Done); GET '/tasks?status=Running' vẫn trả 200 (luồng lọc còn hoạt động)."""
    user = make_user(db_session, role=Role.USER)
    client = client_factory(user=user)

    resp = client.get("/home")
    assert resp.status_code == 200
    assert "/tasks?status=Running" in resp.text
    assert "/tasks?status=Done" in resp.text

    resp2 = client.get("/tasks?status=Running")
    assert resp2.status_code == 200


def test_old_dashboard_path_no_longer_exists(client_factory, db_session):
    """TC-regr: route cũ '/dashboard' đã bị xóa hoàn toàn -> phải trả 404."""
    user = make_user(db_session, role=Role.USER)
    client = client_factory(user=user)

    resp = client.get("/dashboard")
    assert resp.status_code == 404
