"""Kiểm thử tính năng auto-logout tuyệt đối theo từng thiết bị/session sau N giờ kể từ
lúc đăng nhập (độc lập với hoạt động của user), khác với RememberMeMiddleware (chỉ điều
chỉnh Max-Age cookie phía trình duyệt):

- app/routers/auth_router.py::_stamp_login: đóng dấu "login_at" (epoch giây UTC) vào
  session ngay sau khi set session["user_id"] ở CẢ 3 điểm đăng nhập (Google OAuth
  callback, password login, dev-login).
- app/auth.py::_session_expired: hết hạn nếu thiếu/sai kiểu "login_at", hoặc đã quá N
  giờ kể từ login_at - N đọc ĐỘNG từ AppSetting.session_remember_hours (Super Admin đổi
  qua UI có hiệu lực ngay), fallback về .env nếu DB lỗi, luôn KẸP vào
  [session_remember_min_hours, session_remember_max_hours] (.env, default 24-168).
- get_current_user/get_current_user_optional: hết hạn -> clear session + redirect
  login / trả None.

Quy ước dùng chung với tests/test_session_remember_hours.py, tests/test_auth_router.py.
Tránh test biên đúng 0 giây (dùng biên "trong hạn vài giây" cho case chưa hết hạn) để
không flaky. Các test không monkeypatch min/max dùng session_remember_hours trong
khoảng mặc định [24, 168] (Settings.session_remember_min_hours/max_hours) để tránh bị
clamp ngoài ý muốn."""

import json
from base64 import b64encode
from datetime import datetime

import itsdangerous
import pytest

import app.auth as auth_module
from app.auth import RedirectToLogin, _session_expired, get_current_user, get_current_user_optional
from app.config import get_settings
from app.services.app_setting_service import update_app_setting
from tests.conftest import make_user

HOUR = 60 * 60


class _FakeRequest:
    """Stub tối giản thay cho starlette.Request - _session_expired/get_current_user* chỉ
    đụng tới request.session (dict-like có .get/.clear), không cần gì khác từ Request thật."""

    def __init__(self, session: dict):
        self.session = session


def _now() -> int:
    return int(datetime.utcnow().timestamp())


def _sign_session_cookie(session: dict, secret_key: str = "test-secret-key") -> str:
    """Ký cookie "session" giống hệt cách starlette.middleware.sessions.SessionMiddleware
    làm (itsdangerous.TimestampSigner + base64(json)) để giả lập 1 session cookie thật
    của trình duyệt, dùng cho test integration qua TestClient (không đi qua dependency
    override get_current_user)."""
    signer = itsdangerous.TimestampSigner(secret_key)
    data = b64encode(json.dumps(session).encode("utf-8"))
    return signer.sign(data).decode("utf-8")


# ---------------------------------------------------------------------------
# 1. _session_expired: thiếu/sai kiểu login_at, trong hạn, quá hạn
# ---------------------------------------------------------------------------


def test_session_expired_missing_login_at(db_session):
    assert _session_expired(_FakeRequest({}), db_session) is True


def test_session_expired_wrong_type_login_at(db_session):
    assert _session_expired(_FakeRequest({"login_at": "not-a-timestamp"}), db_session) is True


def test_session_expired_within_range_returns_false(db_session):
    update_app_setting(db_session, session_remember_hours=48)
    req = _FakeRequest({"login_at": _now() - 5})  # 5 giay truoc, con xa 48 gio
    assert _session_expired(req, db_session) is False


def test_session_expired_past_n_days_returns_true(db_session):
    update_app_setting(db_session, session_remember_hours=48)
    req = _FakeRequest({"login_at": _now() - 50 * HOUR})
    assert _session_expired(req, db_session) is True


def test_session_expired_24_hours_behaves_like_old_1_day_default(db_session):
    """AC 1b: cau hinh 24 gio (min hop le moi) phai hanh xu tuong duong 1 ngay cu -
    dung dung cong thuc n_hours * 3600 lam nguong so sanh."""
    update_app_setting(db_session, session_remember_hours=24)

    just_within = _FakeRequest({"login_at": _now() - (24 * 3600 - 5)})
    just_past = _FakeRequest({"login_at": _now() - (24 * 3600 + 5)})

    assert _session_expired(just_within, db_session) is False
    assert _session_expired(just_past, db_session) is True


# ---------------------------------------------------------------------------
# 2. N doc dong tu AppSetting; kep [min,max]; fallback khi get_app_setting raise
# ---------------------------------------------------------------------------


def test_session_expired_reads_n_dynamically_from_app_setting(db_session):
    login_at = _now() - 72 * HOUR
    update_app_setting(db_session, session_remember_hours=48)
    assert _session_expired(_FakeRequest({"login_at": login_at}), db_session) is True

    # Super Admin doi N len 100 gio qua UI - cung 1 login_at, session phai lai con hop le
    # NGAY LAP TUC (khong can dang nhap lai) vi N duoc doc dong moi request.
    update_app_setting(db_session, session_remember_hours=100)
    assert _session_expired(_FakeRequest({"login_at": login_at}), db_session) is False


def test_session_expired_clamps_n_above_env_max(db_session, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "session_remember_min_hours", 1)
    monkeypatch.setattr(settings, "session_remember_max_hours", 7)
    # Gia tri AppSetting vuot tran max (vd du lieu cu/loi) - phai bi kep ve 7, KHONG
    # duoc tin dung truc tiep 100 gio.
    update_app_setting(db_session, session_remember_hours=100)

    req = _FakeRequest({"login_at": _now() - 8 * HOUR})  # qua 7 gio (max) nhung chua qua 100
    assert _session_expired(req, db_session) is True


def test_session_expired_clamps_n_below_env_min(db_session, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "session_remember_min_hours", 1)
    monkeypatch.setattr(settings, "session_remember_max_hours", 7)
    # Gia tri AppSetting <= 0 (du lieu bat thuong) - phai bi kep len 1, KHONG duoc coi
    # nhu "khong bao gio het han".
    update_app_setting(db_session, session_remember_hours=-5)

    req = _FakeRequest({"login_at": _now() - 2 * HOUR})  # qua 1 gio (min)
    assert _session_expired(req, db_session) is True


def test_session_expired_fallback_to_env_when_get_app_setting_raises(db_session, monkeypatch):
    def _raise(db=None):
        raise RuntimeError("DB tam thoi khong doc duoc")

    monkeypatch.setattr(auth_module, "get_app_setting", _raise)
    settings = get_settings()
    monkeypatch.setattr(settings, "session_remember_hours", 3)
    monkeypatch.setattr(settings, "session_remember_min_hours", 1)
    monkeypatch.setattr(settings, "session_remember_max_hours", 7)

    expired_req = _FakeRequest({"login_at": _now() - 4 * HOUR})
    assert _session_expired(expired_req, db_session) is True

    valid_req = _FakeRequest({"login_at": _now() - 2 * HOUR})
    assert _session_expired(valid_req, db_session) is False


# ---------------------------------------------------------------------------
# 3. get_current_user / get_current_user_optional
# ---------------------------------------------------------------------------


def test_get_current_user_expired_raises_redirect_and_clears_session(db_session):
    user = make_user(db_session, email="expired-strict@example.com")
    update_app_setting(db_session, session_remember_hours=48)
    session = {"user_id": user.id, "login_at": _now() - 50 * HOUR}
    req = _FakeRequest(session)

    with pytest.raises(RedirectToLogin):
        get_current_user(req, db_session)

    assert session == {}


def test_get_current_user_within_range_returns_user(db_session):
    user = make_user(db_session, email="valid-strict@example.com")
    update_app_setting(db_session, session_remember_hours=48)
    session = {"user_id": user.id, "login_at": _now() - 5}
    req = _FakeRequest(session)

    result = get_current_user(req, db_session)

    assert result.id == user.id


def test_get_current_user_optional_expired_returns_none_and_clears_session(db_session):
    user = make_user(db_session, email="expired-optional@example.com")
    update_app_setting(db_session, session_remember_hours=48)
    session = {"user_id": user.id, "login_at": _now() - 50 * HOUR}
    req = _FakeRequest(session)

    result = get_current_user_optional(req, db_session)

    assert result is None
    assert session == {}


def test_get_current_user_optional_within_range_returns_user(db_session):
    user = make_user(db_session, email="valid-optional@example.com")
    update_app_setting(db_session, session_remember_hours=48)
    session = {"user_id": user.id, "login_at": _now() - 5}
    req = _FakeRequest(session)

    result = get_current_user_optional(req, db_session)

    assert result is not None and result.id == user.id


# ---------------------------------------------------------------------------
# 4. Integration qua cookie ky that (khong dung dependency override get_current_user)
# ---------------------------------------------------------------------------


def test_integration_missing_login_at_redirects_to_login(client_factory, db_session):
    """Cookie session cu tao TRUOC khi tinh nang nay ra doi (khong co login_at) phai bi
    coi la het han - khong duoc mac dinh cho qua vo thoi han."""
    user = make_user(db_session, email="integration-missing@example.com")
    client = client_factory(user=None)
    client.cookies.set("session", _sign_session_cookie({"user_id": user.id}))

    resp = client.get("/home", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_integration_expired_session_redirects_to_login(client_factory, db_session):
    user = make_user(db_session, email="integration-expired@example.com")
    update_app_setting(db_session, session_remember_hours=48)
    client = client_factory(user=None)
    client.cookies.set("session", _sign_session_cookie({"user_id": user.id, "login_at": _now() - 50 * HOUR}))

    resp = client.get("/home", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_integration_valid_session_allows_access(client_factory, db_session):
    user = make_user(db_session, email="integration-valid@example.com")
    update_app_setting(db_session, session_remember_hours=48)
    client = client_factory(user=None)
    client.cookies.set("session", _sign_session_cookie({"user_id": user.id, "login_at": _now() - 5}))

    resp = client.get("/home", follow_redirects=False)

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 5. Cach ly per-user/thiet bi: 2 phien doc lap, 1 het han khong anh huong phien con lai
# ---------------------------------------------------------------------------


def test_integration_expired_session_does_not_affect_other_device(client_factory, db_session):
    user_a = make_user(db_session, email="device-a@example.com")
    user_b = make_user(db_session, email="device-b@example.com")
    update_app_setting(db_session, session_remember_hours=48)

    client_a = client_factory(user=None)
    client_a.cookies.set("session", _sign_session_cookie({"user_id": user_a.id, "login_at": _now() - 50 * HOUR}))

    client_b = client_factory(user=None)
    client_b.cookies.set("session", _sign_session_cookie({"user_id": user_b.id, "login_at": _now() - 5}))

    resp_a = client_a.get("/home", follow_redirects=False)
    resp_b = client_b.get("/home", follow_redirects=False)

    assert resp_a.status_code == 303
    assert resp_a.headers["location"] == "/auth/login"
    assert resp_b.status_code == 200
