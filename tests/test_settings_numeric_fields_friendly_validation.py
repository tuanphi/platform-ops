"""Test hành vi mới (sau khi dev đổi 4 field số ở POST /settings/general từ Form(int|None)
sang Form(str|None) + tự parse bằng `_parse_optional_int`, xem app/routers/settings_router.py
~dòng 60-73) khi nhập chuỗi không phải số nguyên hợp lệ vào 1 trong 4 ô:

- rollback_allowed_seconds, max_concurrent_running_tasks (section="rollback")
- session_remember_hours (section="auth")
- running_alert_after_minutes (section="argocd")

Áp cho cả 2 đường:
- AJAX (header Accept: application/json)   -> 200 + {"ok": false, "message": ...} tiếng
  Việt, message nêu rõ tên ô bị sai (field_label truyền vào _parse_optional_int).
- Form thường (không header)               -> redirect 303 SẠCH (location không có
  query ?ok=/msg=), message lỗi nằm trong flash session, hiển thị qua
  `window.showToast({{ flash_msg | tojson }}, ...)` ở lần GET kế tiếp.

Cả 2 đường đều KHÔNG được ghi giá trị rác vào DB.

Đồng thời test round-trip giá trị hợp lệ vẫn lưu đúng cho cả 4 field/3 section (không hồi
quy) - dev chỉ đổi cách parse input rác, KHÔNG được ảnh hưởng input hợp lệ."""

import json

import pytest

from app.models import Role
from app.services.app_setting_service import get_app_setting
from tests.conftest import make_user

# Mỗi entry: section, field bị test invalid, field_label dùng trong message lỗi (khớp
# _parse_optional_int(..., field_label) trong settings_router.py), payload đầy đủ HỢP LỆ
# của section đó (dùng làm nền, chỉ field đang test bị ghi đè thành giá trị rác/khác), tên
# thuộc tính tương ứng trên AppSetting và giá trị hợp lệ dùng để test round-trip.
FIELD_CASES = [
    pytest.param(
        "rollback",
        "rollback_allowed_seconds",
        "Thời gian cho phép Rollback",
        {"rollback_allowed_seconds": "3600", "max_concurrent_running_tasks": "5"},
        "rollback_allowed_seconds",
        7200,
        id="rollback_allowed_seconds",
    ),
    pytest.param(
        "rollback",
        "max_concurrent_running_tasks",
        "Số task tối đa chạy cùng lúc",
        {"rollback_allowed_seconds": "3600", "max_concurrent_running_tasks": "5"},
        "max_concurrent_running_tasks",
        3,
        id="max_concurrent_running_tasks",
    ),
    pytest.param(
        "auth",
        "session_remember_hours",
        "Số giờ ghi nhớ đăng nhập",
        {"session_remember_hours": "48"},
        "session_remember_hours",
        72,
        id="session_remember_hours",
    ),
    pytest.param(
        "argocd",
        "running_alert_after_minutes",
        "Ngưỡng cảnh báo Running",
        {
            "argocd_url_application": "https://argocd.example.com",
            "argocd_application_postfix": "-stg",
            "running_alert_after_minutes": "30",
            "restart_cooldown_seconds": "60",
        },
        "running_alert_after_minutes",
        45,
        id="running_alert_after_minutes",
    ),
    pytest.param(
        "argocd",
        "restart_cooldown_seconds",
        "Rate limit khi Restart",
        {
            "argocd_url_application": "https://argocd.example.com",
            "argocd_application_postfix": "-stg",
            "running_alert_after_minutes": "30",
            "restart_cooldown_seconds": "60",
        },
        "restart_cooldown_seconds",
        90,
        id="restart_cooldown_seconds",
    ),
]


@pytest.mark.parametrize(
    "section, field, field_label, valid_payload, attr, valid_value",
    FIELD_CASES,
)
def test_non_numeric_ajax_returns_friendly_json_error_and_no_db_change(
    client_factory, db_session, section, field, field_label, valid_payload, attr, valid_value
):
    sa = make_user(db_session, email=f"sa-ajax-{field}@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)
    original = getattr(get_app_setting(db_session), attr)

    payload = {**valid_payload, field: "abc", "section": section}
    resp = client.post("/settings/general", data=payload, headers={"accept": "application/json"})

    assert resp.status_code == 200, "Khong duoc con la 422 tho cua FastAPI Form(int) nua"
    body = resp.json()
    assert body["ok"] is False
    assert body["message"] == f"{field_label} phải là số nguyên hợp lệ", (
        f"Message phai neu ro ten o '{field_label}' bi sai, dung format cua _parse_optional_int"
    )

    assert getattr(get_app_setting(db_session), attr) == original, "DB khong duoc ghi gia tri rac"


@pytest.mark.parametrize(
    "section, field, field_label, valid_payload, attr, valid_value",
    FIELD_CASES,
)
def test_non_numeric_non_ajax_redirects_clean_with_flash_and_no_db_change(
    client_factory, db_session, section, field, field_label, valid_payload, attr, valid_value
):
    sa = make_user(db_session, email=f"sa-noajax-{field}@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)
    original = getattr(get_app_setting(db_session), attr)

    payload = {**valid_payload, field: "abc", "section": section}
    resp = client.post("/settings/general", data=payload, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings/general"
    assert "?" not in resp.headers["location"], "Redirect KHONG duoc chua ?ok=/msg= tren URL (dung flash session)"

    assert getattr(get_app_setting(db_session), attr) == original, "DB khong duoc ghi gia tri rac"

    # Flash duoc render qua window.showToast({{ flash_msg | tojson }}, ...) trong <script> -
    # Jinja tojson escape ky tu co dau thanh \uXXXX, nen phai so bang chinh json.dumps(...)
    # cua CA CAU (khong phai chuoi tho) - dung convention cua
    # tests/test_create_task_flash_session.py. Message chinh xac tu _parse_optional_int khi
    # nhap chuoi khong phai so: f"{field_label} phải là số nguyên hợp lệ".
    page = client.get("/settings/general")
    assert page.status_code == 200
    expected_message = f"{field_label} phải là số nguyên hợp lệ"
    assert json.dumps(expected_message) in page.text


@pytest.mark.parametrize(
    "section, field, field_label, valid_payload, attr, valid_value",
    FIELD_CASES,
)
def test_valid_value_still_saved_correctly_no_regression(
    client_factory, db_session, section, field, field_label, valid_payload, attr, valid_value
):
    sa = make_user(db_session, email=f"sa-valid-{field}@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)

    payload = {**valid_payload, field: str(valid_value), "section": section}
    resp = client.post("/settings/general", data=payload, headers={"accept": "application/json"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True, f"Gia tri hop le bi tu choi nham: {body}"
    assert getattr(get_app_setting(db_session), attr) == valid_value
