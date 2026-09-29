"""Happy path: favicon toàn app đã đổi từ logo Gpay (/static/favicon.png) sang
SVG emoji tên lửa (🚀) giống trang login, do base.html được sửa lại đồng bộ
với login.html.
"""

from app.models import Role
from tests.conftest import make_user

ROCKET_FAVICON_MARKER = "%F0%9F%9A%80"  # 🚀 percent-encoded trong data URI SVG
OLD_FAVICON_LINK = '<link rel="icon" type="image/png" href="/static/favicon.png">'


def test_tasks_page_has_rocket_favicon_no_old_gpay_favicon(client_factory, db_session):
    user = make_user(db_session, role=Role.USER)
    client = client_factory(user=user)

    resp = client.get("/tasks")

    assert resp.status_code == 200
    assert ROCKET_FAVICON_MARKER in resp.text
    assert OLD_FAVICON_LINK not in resp.text


def test_home_page_also_has_rocket_favicon(client_factory, db_session):
    user = make_user(db_session, role=Role.USER)
    client = client_factory(user=user)

    resp = client.get("/home")

    assert resp.status_code == 200
    assert ROCKET_FAVICON_MARKER in resp.text
    assert OLD_FAVICON_LINK not in resp.text


def test_login_page_still_has_rocket_favicon_no_regression(client_factory):
    client = client_factory(user=None)

    resp = client.get("/auth/login")

    assert resp.status_code == 200
    assert ROCKET_FAVICON_MARKER in resp.text
