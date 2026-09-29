"""Kiểm thử tính năng SESSION_REMEMBER_HOURS sửa được qua UI /settings/general (Super
Admin) thay vì chỉ .env + restart:

- Seed lần đầu vào bảng singleton app_setting bị CLAMP vào [min,max] đọc từ .env
  (app_setting_service._seed_from_env).
- POST /settings/general (section=auth) validate server-side theo ngưỡng
  SESSION_REMEMBER_MIN_HOURS/MAX_HOURS (.env, KHÔNG hardcode) - chỉ Super Admin được sửa.
- Giá trị mới áp dụng NGAY cho session mới (và cả session đang đăng nhập sẵn) mà không
  cần restart app, thông qua RememberMeMiddleware đọc get_app_setting() mỗi response.

Đơn vị đã đổi từ NGÀY sang GIỜ (default 168h = 7 ngày cũ, min 24h, max 168h) - xem
app/config.py Settings.session_remember_hours/min_hours/max_hours.

Quy ước dùng chung với các test khác trong thư mục: `client_factory` override
get_current_user cho các route chỉ cần kiểm tra permission/persist DB (không đi qua
cookie thật); riêng test Max-Age cookie phải đăng nhập thật qua /auth/dev-login để
SessionMiddleware/RememberMeMiddleware nhận được `request.scope["session"]` thật sự
(dependency override KHÔNG giả lập được cookie session)."""

import re

import pytest

from app.config import get_settings
from app.models import Role
from app.services.app_setting_service import _seed_from_env, get_app_setting, update_app_setting
from tests.conftest import make_user

MAX_AGE_RE = re.compile(r"Max-Age=(\d+)", re.IGNORECASE)


def _session_cookie_max_age(set_cookie_headers) -> int | None:
    for raw in set_cookie_headers:
        if raw.startswith("session="):
            m = MAX_AGE_RE.search(raw)
            return int(m.group(1)) if m else None
    return None


CSRF_INPUT_RE = re.compile(r'name="csrf_token" value="([^"]*)"')


def _csrf_token(client) -> str:
    """GET /auth/login de lay csrf_token hop le (fix login CSRF - login/dev-login gio
    bat buoc token nay khop voi token luu trong session)."""
    resp = client.get("/auth/login")
    match = CSRF_INPUT_RE.search(resp.text)
    assert match, "khong tim thay hidden input csrf_token trong login.html"
    return match.group(1)


# ---------------------------------------------------------------------------
# 1. Seed lần đầu từ .env + CLAMP vào [min,max]
# ---------------------------------------------------------------------------


def test_seed_from_env_uses_settings_value_when_in_range(db_session, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "session_remember_hours", 48)
    monkeypatch.setattr(settings, "session_remember_min_hours", 24)
    monkeypatch.setattr(settings, "session_remember_max_hours", 168)

    setting = get_app_setting(db_session)

    assert setting.session_remember_hours == 48


def test_seed_from_env_clamps_value_above_max(db_session, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "session_remember_hours", 1000)
    monkeypatch.setattr(settings, "session_remember_min_hours", 24)
    monkeypatch.setattr(settings, "session_remember_max_hours", 168)

    setting = get_app_setting(db_session)

    assert setting.session_remember_hours == 168


def test_seed_from_env_clamps_value_below_min():
    """Gọi thẳng _seed_from_env() (không qua get_app_setting/DB) để test thuần logic
    clamp, tránh phụ thuộc unique-constraint của bảng singleton khi seed 2 lần."""
    settings = get_settings()
    import app.services.app_setting_service as svc

    original = (
        settings.session_remember_hours,
        settings.session_remember_min_hours,
        settings.session_remember_max_hours,
    )
    try:
        settings.session_remember_hours = 0
        settings.session_remember_min_hours = 24
        settings.session_remember_max_hours = 168
        seeded = svc._seed_from_env()
        assert seeded.session_remember_hours == 24
    finally:
        (
            settings.session_remember_hours,
            settings.session_remember_min_hours,
            settings.session_remember_max_hours,
        ) = original


def test_seed_from_env_clamps_negative_value():
    settings = get_settings()
    original = settings.session_remember_hours
    try:
        settings.session_remember_hours = -5
        settings.session_remember_min_hours = 24
        settings.session_remember_max_hours = 168
        seeded = _seed_from_env()
        assert seeded.session_remember_hours == 24
    finally:
        settings.session_remember_hours = original


# ---------------------------------------------------------------------------
# 2. POST /settings/general (section=auth) lưu đúng DB - chỉ Super Admin
# ---------------------------------------------------------------------------


def test_super_admin_can_update_session_remember_hours(client_factory, db_session):
    sa = make_user(db_session, email="sa@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)

    resp = client.post(
        "/settings/general",
        data={"section": "auth", "session_remember_hours": 48},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert get_app_setting(db_session).session_remember_hours == 48


@pytest.mark.parametrize("role", [Role.USER, Role.ADMIN])
def test_non_super_admin_cannot_update_session_remember_hours(client_factory, db_session, role):
    user = make_user(db_session, email=f"{role.value}@example.com", role=role)
    client = client_factory(user=user)

    original = get_app_setting(db_session).session_remember_hours

    resp = client.post(
        "/settings/general",
        data={"section": "auth", "session_remember_hours": 48},
        follow_redirects=False,
    )

    assert resp.status_code == 403
    assert get_app_setting(db_session).session_remember_hours == original


def test_unauthenticated_cannot_update_session_remember_hours(client_factory, db_session):
    client = client_factory(user=None)
    original = get_app_setting(db_session).session_remember_hours

    resp = client.post(
        "/settings/general",
        data={"section": "auth", "session_remember_hours": 48},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"
    assert get_app_setting(db_session).session_remember_hours == original


# ---------------------------------------------------------------------------
# 4. Validate REJECT server-side ngoài [min,max] - default .env: min=24, max=168
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_value", [0, -1, 10, 200])
def test_out_of_range_int_values_rejected(client_factory, db_session, bad_value):
    sa = make_user(db_session, email="sa@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)
    original = get_app_setting(db_session).session_remember_hours

    resp = client.post(
        "/settings/general",
        data={"section": "auth", "session_remember_hours": bad_value},
        follow_redirects=False,
        headers={"accept": "application/json"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["message"] == "Số giờ ghi nhớ đăng nhập phải trong khoảng 24-168"
    assert get_app_setting(db_session).session_remember_hours == original


@pytest.mark.parametrize("bad_value", ["abc", "3.5"])
def test_non_integer_values_rejected_friendly_no_db_change(client_factory, db_session, bad_value):
    """Sau khi dev doi session_remember_hours sang str|None + _parse_optional_int
    (app/routers/settings_router.py), chuoi khong parse duoc thanh int (chu, so thap
    phan) KHONG con tra 422 tho cua FastAPI nua. Duong AJAX (Accept: application/json)
    tra 200 + {'ok': False, 'message': ...} tieng Viet nêu ro ten o; DB phai giu nguyen."""
    sa = make_user(db_session, email="sa@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)
    original = get_app_setting(db_session).session_remember_hours

    resp = client.post(
        "/settings/general",
        data={"section": "auth", "session_remember_hours": bad_value},
        follow_redirects=False,
        headers={"accept": "application/json"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "Số giờ ghi nhớ đăng nhập" in body["message"]
    assert get_app_setting(db_session).session_remember_hours == original


def test_empty_value_rejected_no_db_change_and_no_silent_reset(client_factory, db_session):
    """Ô trống ("") KHÔNG được coi là "không đổi" và bị lặng lẽ bỏ qua, cũng KHÔNG được
    ghi None/0 vào DB: _parse_optional_int("") trả None, và nhánh "auth" coi None là
    ngoài khoảng [min,max] hợp lệ -> bị từ chối như mọi giá trị rác khác, DB giữ nguyên.
    Xác nhận hành vi này qua cả 2 đường AJAX và form thường (không có header Accept)."""
    sa = make_user(db_session, email="sa-empty@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)
    original = get_app_setting(db_session).session_remember_hours

    resp_ajax = client.post(
        "/settings/general",
        data={"section": "auth", "session_remember_hours": ""},
        follow_redirects=False,
        headers={"accept": "application/json"},
    )
    assert resp_ajax.status_code == 200
    body = resp_ajax.json()
    assert body["ok"] is False
    assert get_app_setting(db_session).session_remember_hours == original

    resp_form = client.post(
        "/settings/general",
        data={"section": "auth", "session_remember_hours": ""},
        follow_redirects=False,
    )
    assert resp_form.status_code == 303
    assert resp_form.headers["location"] == "/settings/general"
    assert get_app_setting(db_session).session_remember_hours == original


def test_missing_field_rejected(client_factory, db_session):
    sa = make_user(db_session, email="sa@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)
    original = get_app_setting(db_session).session_remember_hours

    resp = client.post(
        "/settings/general",
        data={"section": "auth"},
        follow_redirects=False,
        headers={"accept": "application/json"},
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert get_app_setting(db_session).session_remember_hours == original


@pytest.mark.parametrize("boundary_value", [24, 168])
def test_boundary_values_accepted(client_factory, db_session, boundary_value):
    sa = make_user(db_session, email="sa@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)

    resp = client.post(
        "/settings/general",
        data={"section": "auth", "session_remember_hours": boundary_value},
        follow_redirects=False,
        headers={"accept": "application/json"},
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert get_app_setting(db_session).session_remember_hours == boundary_value


# ---------------------------------------------------------------------------
# 5. Ngưỡng min/max đọc từ .env (không hardcode) - đổi env giả lập rồi test lại
# ---------------------------------------------------------------------------


def test_validation_bound_follows_env_max_not_hardcoded(client_factory, db_session, monkeypatch):
    settings = get_settings()
    # Doi ca min va max de dam bao khoang hop le (min mac dinh that = 24 > 3 neu chi
    # doi max se lam khoang [24,3] vo nghia va luon reject).
    monkeypatch.setattr(settings, "session_remember_min_hours", 1)
    monkeypatch.setattr(settings, "session_remember_max_hours", 3)
    sa = make_user(db_session, email="sa@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)

    # 4 vuot qua max=3 moi phai bi tu choi.
    resp = client.post(
        "/settings/general",
        data={"section": "auth", "session_remember_hours": 4},
        follow_redirects=False,
        headers={"accept": "application/json"},
    )
    assert resp.json()["ok"] is False
    assert get_app_setting(db_session).session_remember_hours != 4

    # 3 (= max moi) van duoc chap nhan.
    resp_ok = client.post(
        "/settings/general",
        data={"section": "auth", "session_remember_hours": 3},
        follow_redirects=False,
        headers={"accept": "application/json"},
    )
    assert resp_ok.json()["ok"] is True
    assert get_app_setting(db_session).session_remember_hours == 3


def test_general_page_renders_env_min_max_in_input_attrs(client_factory, db_session, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "session_remember_min_hours", 2)
    monkeypatch.setattr(settings, "session_remember_max_hours", 3)
    sa = make_user(db_session, email="sa@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)

    resp = client.get("/settings/general")

    assert resp.status_code == 200
    assert 'name="session_remember_hours"' in resp.text
    assert 'min="2"' in resp.text
    assert 'max="3"' in resp.text


# ---------------------------------------------------------------------------
# 3 & 6. Áp dụng ngay cho session MỚI, không phá session cũ đang đăng nhập
# ---------------------------------------------------------------------------


def test_new_login_cookie_max_age_reflects_current_db_value(client_factory, db_session, monkeypatch):
    """Đăng nhập thật qua /auth/dev-login (AUTH_DEV_MODE=true trong test env) để
    RememberMeMiddleware xử lý request.scope['session'] thật, không phải dependency
    override - rồi so Max-Age trên Set-Cookie với session_remember_hours trong DB.

    RememberMeMiddleware tự mở session riêng qua get_app_setting() KHÔNG truyền db
    (giống notify_service/telegram_router) - trỏ vào app.database.SessionLocal thật
    (không phải db_session in-memory riêng của test), nên phải monkeypatch app.auth.
    get_app_setting sang db_session của test, theo đúng convention của
    tests/test_notify_service.py và tests/test_telegram_router.py."""
    import app.auth as auth_module
    import app.routers.auth_router as auth_router

    monkeypatch.setattr(auth_router.settings, "auth_dev_mode", True)
    monkeypatch.setattr(auth_module, "get_app_setting", lambda: get_app_setting(db_session))
    update_app_setting(db_session, session_remember_hours=48)

    client = client_factory(user=None)
    csrf_token = _csrf_token(client)
    resp = client.post(
        "/auth/dev-login", data={"email": "newlogin@example.com", "csrf_token": csrf_token}, follow_redirects=False
    )

    assert resp.status_code == 303
    max_age = _session_cookie_max_age(resp.headers.get_list("set-cookie"))
    assert max_age == 48 * 3600


def test_existing_session_survives_value_change_and_gets_new_max_age(client_factory, db_session, monkeypatch):
    """User đã đăng nhập trước khi Super Admin đổi giá trị: request kế tiếp của
    user cũ vẫn hoạt động bình thường (không bị đá ra / mất quyền truy cập), và
    Max-Age của cookie session được cập nhật theo giá trị mới ngay lập tức."""
    import app.auth as auth_module
    import app.routers.auth_router as auth_router

    monkeypatch.setattr(auth_router.settings, "auth_dev_mode", True)
    monkeypatch.setattr(auth_module, "get_app_setting", lambda: get_app_setting(db_session))
    update_app_setting(db_session, session_remember_hours=168)

    client = client_factory(user=None)
    csrf_token = _csrf_token(client)
    login_resp = client.post(
        "/auth/dev-login", data={"email": "existing@example.com", "csrf_token": csrf_token}, follow_redirects=False
    )
    assert login_resp.status_code == 303
    max_age_before = _session_cookie_max_age(login_resp.headers.get_list("set-cookie"))
    assert max_age_before == 168 * 3600

    # Super Admin doi gia tri sau khi user tren da dang nhap.
    update_app_setting(db_session, session_remember_hours=48)

    # Request ke tiep cua user cu (TestClient tu giu cookie session vua nhan duoc)
    # phai van hoat dong binh thuong (khong bi dang xuat), va Max-Age cap nhat ngay.
    next_resp = client.get("/auth/login", follow_redirects=False)
    # Da dang nhap nen /auth/login redirect ve "/" (khong bi vang ve login lai).
    assert next_resp.status_code == 303
    assert next_resp.headers["location"] == "/"
    max_age_after = _session_cookie_max_age(next_resp.headers.get_list("set-cookie"))
    assert max_age_after == 48 * 3600


def test_remember_not_ticked_becomes_session_cookie_regardless_of_db_value(client_factory, db_session, monkeypatch):
    """Khi user KHÔNG tick "Ghi nhớ đăng nhập" lúc login bằng password, cookie phải
    biến thành session cookie (không có Max-Age) bất kể session_remember_hours trong
    DB là bao nhiêu - regression cho nhánh remember=False của RememberMeMiddleware."""
    import app.routers.auth_router as auth_router
    from app.services.password_service import hash_password

    monkeypatch.setattr(auth_router.settings, "auth_enable_password", True)
    update_app_setting(db_session, session_remember_hours=120)
    make_user(db_session, email="norm@example.com", role=Role.USER, password_hash=hash_password("pw12345"))

    client = client_factory(user=None)
    csrf_token = _csrf_token(client)
    resp = client.post(
        "/auth/login",
        data={"email": "norm@example.com", "password": "pw12345", "csrf_token": csrf_token},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    max_age = _session_cookie_max_age(resp.headers.get_list("set-cookie"))
    assert max_age is None


# ---------------------------------------------------------------------------
# Regression: model default / migration default nhất quán với đặc tả tính năng
# ---------------------------------------------------------------------------


def test_app_setting_model_default_matches_code_level_settings_default():
    """AppSetting.session_remember_hours.default (dùng khi tạo object KHÔNG qua
    _seed_from_env, vd insert thủ công/test khác) phải khớp với default KHAI BÁO
    Ở MỨC CODE của Settings.session_remember_hours trong app/config.py (spec: 168h)
    - nếu lệch là bug tiềm ẩn (session mới sẽ có TTL khác spec nếu code nào đó bỏ
    qua _seed_from_env). Cố tình lấy default qua Settings.model_fields (không qua
    get_settings()/instance) để KHÔNG bị giá trị SESSION_REMEMBER_HOURS trong .env
    thật của máy dev che giấu sai lệch này."""
    from app.config import Settings
    from app.models import AppSetting

    code_level_default = Settings.model_fields["session_remember_hours"].default
    model_default = AppSetting.__table__.c.session_remember_hours.default.arg

    assert model_default == code_level_default, (
        f"AppSetting.session_remember_hours default={model_default} khac voi "
        f"Settings.session_remember_hours default khai bao trong app/config.py="
        f"{code_level_default} - kiem tra lai app/models.py"
    )
