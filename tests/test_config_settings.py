"""Kiểm thử validator production-safety trong app/config.py (fix B2/B3).

Bug gốc: AUTH_DEV_MODE=true (cho phép đăng nhập giả mạo bất kỳ email nào, kể cả
admin/super_admin) hoặc SECRET_KEY mặc định "change-me-in-prod" vẫn có thể chạy
được ở production (APP_URL không phải localhost) mà không có cảnh báo/chặn nào.
Fix thêm `model_validator(mode="after")` raise ValueError fail-fast khi phát hiện
tổ hợp cấu hình nguy hiểm này.

Settings() được khởi tạo trực tiếp bằng kwargs trong từng test (pydantic-settings
ưu tiên init kwargs cao hơn biến môi trường) để không phụ thuộc/đụng tới các biến
env do tests/conftest.py set sẵn (DATABASE_URL, SECRET_KEY, AUTH_DEV_MODE)."""

import pytest

from app.config import DEFAULT_SECRET_KEY, Settings


# ---------------------------------------------------------------------------
# Phải RAISE - cấu hình nguy hiểm bị nghi là production
# ---------------------------------------------------------------------------


def test_auth_dev_mode_true_with_non_localhost_app_url_raises():
    with pytest.raises(ValueError, match="AUTH_DEV_MODE"):
        Settings(
            app_url="https://deploy.example.com",
            auth_dev_mode=True,
            secret_key="a-real-random-secret-value",
        )


def test_default_secret_key_with_non_localhost_app_url_raises():
    with pytest.raises(ValueError, match="SECRET_KEY"):
        Settings(
            app_url="https://deploy.example.com",
            auth_dev_mode=False,
            secret_key=DEFAULT_SECRET_KEY,
        )


def test_auth_dev_mode_true_and_default_secret_key_both_raise_on_first_check():
    """Cả 2 điều kiện nguy hiểm cùng lúc - validator raise ở check AUTH_DEV_MODE
    trước (thứ tự if trong _enforce_production_safety), không được im lặng bỏ qua."""
    with pytest.raises(ValueError, match="AUTH_DEV_MODE"):
        Settings(
            app_url="https://deploy.example.com",
            auth_dev_mode=True,
            secret_key=DEFAULT_SECRET_KEY,
        )


def test_http_scheme_non_localhost_still_raises():
    """Không phải cứ http:// là an toàn - chỉ localhost/127.0.0.1/... mới được coi
    là dev local, http://internal.example.com vẫn bị coi là nghi production."""
    with pytest.raises(ValueError, match="AUTH_DEV_MODE"):
        Settings(app_url="http://internal.example.com", auth_dev_mode=True, secret_key="x")


def test_empty_app_url_treated_as_non_local_and_still_raises():
    """APP_URL rỗng/misconfig (hostname parse ra rỗng) không được coi nhầm là
    localhost - phải fail-closed (raise) chứ không fail-open."""
    with pytest.raises(ValueError, match="AUTH_DEV_MODE"):
        Settings(app_url="", auth_dev_mode=True, secret_key="a-real-random-secret-value")


def test_localhost_app_url_but_mysql_database_url_raises():
    """Fix mở rộng: chỉ APP_URL=localhost không còn đủ để coi là "local" - nếu
    DATABASE_URL là MySQL (dấu hiệu production thật, quên sửa APP_URL khi deploy) vẫn
    phải raise, tránh 1 biến duy nhất (APP_URL) vô hiệu cả 2 chốt an toàn."""
    with pytest.raises(ValueError, match="AUTH_DEV_MODE"):
        Settings(
            app_url="http://localhost:8000",
            auth_dev_mode=True,
            secret_key=DEFAULT_SECRET_KEY,
            database_url="mysql+pymysql://user:pass@prod-db:3306/form_deploy",
        )


# ---------------------------------------------------------------------------
# KHÔNG được raise - cấu hình hợp lệ
# ---------------------------------------------------------------------------


def test_localhost_app_url_allows_dev_mode_and_default_secret():
    settings = Settings(
        app_url="http://localhost:8000",
        auth_dev_mode=True,
        secret_key=DEFAULT_SECRET_KEY,
    )
    assert settings.auth_dev_mode is True
    assert settings.secret_key == DEFAULT_SECRET_KEY


def test_127_0_0_1_app_url_allows_default_secret():
    settings = Settings(
        app_url="http://127.0.0.1:8000",
        auth_dev_mode=False,
        secret_key=DEFAULT_SECRET_KEY,
    )
    assert settings.secret_key == DEFAULT_SECRET_KEY


def test_localhost_hostname_case_insensitive():
    settings = Settings(app_url="http://LOCALHOST:8000", auth_dev_mode=True, secret_key=DEFAULT_SECRET_KEY)
    assert settings.is_local_app_url() is True


def test_production_url_with_dev_mode_off_and_real_secret_does_not_raise():
    settings = Settings(
        app_url="https://deploy.example.com",
        auth_dev_mode=False,
        secret_key="a-real-random-secret-value-not-default",
    )
    assert settings.auth_dev_mode is False
    assert settings.secret_key != DEFAULT_SECRET_KEY


# ---------------------------------------------------------------------------
# is_local_app_url() - heuristic nhận diện dev local
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "app_url,expected",
    [
        ("http://localhost:8000", True),
        ("http://127.0.0.1:8000", True),
        ("http://0.0.0.0:8000", True),
        ("http://[::1]:8000", True),
        ("https://deploy.example.com", False),
        ("http://deploy.example.com", False),
    ],
)
def test_is_local_app_url_hostnames(app_url, expected):
    # Dùng secret_key/auth_dev_mode an toàn để không kích hoạt validator raise khi
    # expected=False (chỉ muốn test riêng logic is_local_app_url()).
    settings = Settings(
        app_url=app_url,
        auth_dev_mode=False,
        secret_key="a-real-random-secret-value-not-default",
    )
    assert settings.is_local_app_url() is expected
