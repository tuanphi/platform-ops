"""Kiểm thử SessionMiddleware(https_only=...) trong app/main.py (fix B6).

Bug gốc: cookie session không set cờ Secure, có thể bị gửi qua HTTP thường ở
production (sniffing/MITM). Fix: `https_only=settings.app_url.startswith("https://")`
- chỉ bật Secure cookie khi APP_URL thật sự là https://, vẫn cho phép dev local
qua http://localhost hoạt động bình thường.

`app = FastAPI(...)` và `app.add_middleware(SessionMiddleware, ...)` chạy 1 lần
lúc import module (bind cứng theo settings.app_url tại thời điểm import, không
đọc lại mỗi request), và `get_settings()` dùng `@lru_cache` (singleton toàn tiến
trình) - nên KHÔNG thể re-import app.main với APP_URL khác trong cùng tiến trình
pytest (module đã bị cache trong sys.modules, và Settings() cache trong lru_cache
dùng chung với các test khác trong cùng session). Test dưới đây dùng 2 cách:

1. Runtime check trên `app.main` đã import sẵn (session hiện tại): xác nhận
   `https_only` thực tế đang bind trong app.user_middleware khớp với công thức
   `settings.app_url.startswith("https://")` của Settings đang chạy.
2. Subprocess Python riêng biệt (tiến trình mới, sys.modules/lru_cache sạch) với
   APP_URL=https://... và APP_URL=http://... khác localhost để xác nhận cả 2
   nhánh true/false của https_only thực sự được áp dụng khi khởi động app thật,
   không chỉ đúng về mặt công thức tách rời."""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _session_middleware_kwargs(app):
    for mw in app.user_middleware:
        if mw.cls.__name__ == "SessionMiddleware":
            return mw.kwargs
    raise AssertionError("SessionMiddleware không được đăng ký trong app.user_middleware")


def test_https_only_matches_app_url_scheme_for_currently_imported_app():
    import app.main as main_module
    from app.config import get_settings

    settings = get_settings()
    kwargs = _session_middleware_kwargs(main_module.app)

    assert kwargs["https_only"] == settings.app_url.startswith("https://")


_BOOT_SCRIPT = """
import os
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "a-safe-real-secret-value-for-subprocess-test"
os.environ["AUTH_DEV_MODE"] = "false"
os.environ["APP_URL"] = {app_url!r}
import app.main as m
mw = next(x for x in m.app.user_middleware if x.cls.__name__ == "SessionMiddleware")
print("HTTPS_ONLY=" + str(mw.kwargs["https_only"]))
"""


def _boot_app_in_subprocess(app_url: str) -> bool:
    """Khởi động app.main trong 1 tiến trình Python mới (tránh lru_cache/sys.modules
    dính từ tiến trình pytest chính) với APP_URL cho trước, trả về https_only thực
    tế mà SessionMiddleware nhận được."""
    result = subprocess.run(
        [sys.executable, "-c", _BOOT_SCRIPT.format(app_url=app_url)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"subprocess lỗi:\nstdout={result.stdout}\nstderr={result.stderr}"
    for line in result.stdout.splitlines():
        if line.startswith("HTTPS_ONLY="):
            return line.split("=", 1)[1] == "True"
    raise AssertionError(f"Không tìm thấy HTTPS_ONLY trong output: {result.stdout}")


@pytest.mark.parametrize(
    "app_url,expected_https_only",
    [
        ("https://deploy.example.com", True),
        ("http://deploy.example.com", False),  # http, không phải localhost
        ("http://localhost:8000", False),  # dev local hợp lệ
    ],
)
def test_https_only_wired_correctly_on_real_app_boot(app_url, expected_https_only):
    assert _boot_app_in_subprocess(app_url) is expected_https_only
