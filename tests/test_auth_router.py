"""Kiểm thử app/routers/auth_router.py: dev-login, password-login, logout."""

import re

from app.models import Role
from app.services.password_service import hash_password
from tests.conftest import make_user


def _csrf_token(client):
    """GET /auth/login để lấy csrf_token hợp lệ (server lưu trong session, embed vào
    hidden input) - login/dev-login giờ bắt buộc token này khớp (fix login CSRF)."""
    resp = client.get("/auth/login")
    match = re.search(r'name="csrf_token" value="([^"]*)"', resp.text)
    assert match, "khong tim thay hidden input csrf_token trong login.html"
    return match.group(1)


def test_dev_login_disabled_redirects_to_login(client_factory, monkeypatch):
    import app.routers.auth_router as auth_router

    monkeypatch.setattr(auth_router.settings, "auth_dev_mode", False)
    client = client_factory(user=None)

    # auth_dev_mode=False bi chan TRUOC ca buoc kiem CSRF nen khong can token that.
    resp = client.post(
        "/auth/dev-login", data={"email": "someone@example.com", "csrf_token": "x"}, follow_redirects=False
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_dev_login_creates_user_with_default_role(client_factory, db_session, monkeypatch):
    import app.routers.auth_router as auth_router

    monkeypatch.setattr(auth_router.settings, "auth_dev_mode", True)
    client = client_factory(user=None)
    csrf_token = _csrf_token(client)

    resp = client.post(
        "/auth/dev-login",
        data={"email": "newperson@example.com", "csrf_token": csrf_token},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    from app.models import User

    created = db_session.query(User).filter(User.email == "newperson@example.com").first()
    assert created is not None
    assert created.role == Role.USER


def test_dev_login_wrong_csrf_token_rejected(client_factory, db_session, monkeypatch):
    """Login CSRF: token khong khop voi token trong session phai bi tu choi, khong duoc
    tao user/dang nhap."""
    import app.routers.auth_router as auth_router

    monkeypatch.setattr(auth_router.settings, "auth_dev_mode", True)
    client = client_factory(user=None)
    _csrf_token(client)  # khoi tao session (co csrf_token that trong session)

    resp = client.post(
        "/auth/dev-login",
        data={"email": "forged@example.com", "csrf_token": "token-gia-mao-khong-khop"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"
    from app.models import User

    assert db_session.query(User).filter(User.email == "forged@example.com").first() is None


def test_password_login_disabled_by_default(client_factory):
    client = client_factory(user=None)
    # auth_enable_password=False (mac dinh) bi chan TRUOC ca buoc kiem CSRF.
    resp = client.post(
        "/auth/login",
        data={"email": "x@example.com", "password": "whatever", "csrf_token": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_password_login_wrong_password_rejected(client_factory, db_session, monkeypatch):
    import app.routers.auth_router as auth_router

    monkeypatch.setattr(auth_router.settings, "auth_enable_password", True)
    user = make_user(db_session, email="pwuser@example.com", role=Role.USER, password_hash=hash_password("correct-horse"))
    client = client_factory(user=None)
    csrf_token = _csrf_token(client)

    resp = client.post(
        "/auth/login",
        data={"email": "pwuser@example.com", "password": "wrong-password", "csrf_token": csrf_token},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_password_login_inactive_user_rejected(client_factory, db_session, monkeypatch):
    import app.routers.auth_router as auth_router

    monkeypatch.setattr(auth_router.settings, "auth_enable_password", True)
    make_user(
        db_session,
        email="inactive@example.com",
        role=Role.USER,
        password_hash=hash_password("correct-horse"),
        is_active=False,
    )
    client = client_factory(user=None)
    csrf_token = _csrf_token(client)

    resp = client.post(
        "/auth/login",
        data={"email": "inactive@example.com", "password": "correct-horse", "csrf_token": csrf_token},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_password_login_no_password_hash_set_rejected(client_factory, db_session, monkeypatch):
    """User được tạo qua Google OAuth / dev-login chưa từng set password (password_hash
    None) - đăng nhập password phải bị từ chối, không được lỗi 500."""
    import app.routers.auth_router as auth_router

    monkeypatch.setattr(auth_router.settings, "auth_enable_password", True)
    make_user(db_session, email="nopass@example.com", role=Role.USER)
    client = client_factory(user=None)
    csrf_token = _csrf_token(client)

    resp = client.post(
        "/auth/login",
        data={"email": "nopass@example.com", "password": "anything", "csrf_token": csrf_token},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_password_login_correct_credentials_accepted_with_valid_csrf(client_factory, db_session, monkeypatch):
    """Regression: dang nhap dung mat khau + dung csrf_token van phai thanh cong binh
    thuong (fix CSRF khong duoc lam gay luong dang nhap hop le)."""
    import app.routers.auth_router as auth_router

    monkeypatch.setattr(auth_router.settings, "auth_enable_password", True)
    make_user(db_session, email="gooduser@example.com", role=Role.USER, password_hash=hash_password("correct-horse"))
    client = client_factory(user=None)
    csrf_token = _csrf_token(client)

    resp = client.post(
        "/auth/login",
        data={"email": "gooduser@example.com", "password": "correct-horse", "csrf_token": csrf_token},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_password_login_rate_limited_after_repeated_failures(client_factory, db_session, monkeypatch):
    """Fix bao mat: sau qua nhieu lan sai lien tiep, dang nhap (ke ca dung mat khau) phai
    bi khoa tam thoi - chan brute-force."""
    import app.routers.auth_router as auth_router

    monkeypatch.setattr(auth_router.settings, "auth_enable_password", True)
    monkeypatch.setattr(auth_router, "_LOGIN_MAX_ATTEMPTS", 3)
    make_user(db_session, email="ratelimit@example.com", role=Role.USER, password_hash=hash_password("correct-horse"))
    client = client_factory(user=None)

    for _ in range(3):
        csrf_token = _csrf_token(client)
        client.post(
            "/auth/login",
            data={"email": "ratelimit@example.com", "password": "wrong", "csrf_token": csrf_token},
            follow_redirects=False,
        )

    csrf_token = _csrf_token(client)
    resp = client.post(
        "/auth/login",
        data={"email": "ratelimit@example.com", "password": "correct-horse", "csrf_token": csrf_token},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_logout_clears_session(client_factory, db_session):
    user = make_user(db_session, role=Role.USER)
    client = client_factory(user=user)

    resp = client.get("/auth/logout", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


# ---------------------------------------------------------------------------
# _login_attempts (rate limit login) - bo nho phai co TRAN CUNG (LRU), khong duoc phinh
# vo han neu attacker flood nhieu email KHAC NHAU (fix theo audit lan 2).
# ---------------------------------------------------------------------------


def test_login_attempts_state_bounded_by_lru_eviction(monkeypatch):
    """Flood nhieu email khac nhau vuot qua _LOGIN_ATTEMPT_STATE_MAX_ENTRIES phai KHONG
    lam dict phinh vo han - entry cu nhat (LRU) bi loai bo O(1), khong con giu toan bo
    lich su."""
    import app.routers.auth_router as auth_router

    monkeypatch.setattr(auth_router, "_LOGIN_ATTEMPT_STATE_MAX_ENTRIES", 10)
    auth_router._login_attempts.clear()

    for i in range(50):
        auth_router._record_login_failure(f"flood{i}@example.com")

    assert len(auth_router._login_attempts) == 10
    # 10 email GAN NHAT phai con, cac email dau tien (cu nhat) phai bi loai.
    assert "flood0@example.com" not in auth_router._login_attempts
    assert "flood49@example.com" in auth_router._login_attempts


def test_login_attempts_recently_touched_entry_survives_eviction_longer(monkeypatch):
    """LRU dung: ghi nhan that bai cho 'victim', flood vai email khac, roi ghi nhan THEM
    1 that bai nua cho victim (move_to_end -> day ra cuoi hang doi) - victim phai song sot
    qua dot flood tiep theo, trong khi cac email flood DAU TIEN (khong duoc lam moi) bi
    loai truoc."""
    import app.routers.auth_router as auth_router

    monkeypatch.setattr(auth_router, "_LOGIN_ATTEMPT_STATE_MAX_ENTRIES", 10)
    auth_router._login_attempts.clear()

    auth_router._record_login_failure("victim@example.com")
    for i in range(5):
        auth_router._record_login_failure(f"flood{i}@example.com")
    auth_router._record_login_failure("victim@example.com")  # lam moi, day victim ra cuoi
    for i in range(5, 14):
        auth_router._record_login_failure(f"flood{i}@example.com")

    assert "victim@example.com" in auth_router._login_attempts
    assert "flood0@example.com" not in auth_router._login_attempts
