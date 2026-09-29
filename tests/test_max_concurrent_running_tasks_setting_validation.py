"""Test validate của setting mới AppSetting.max_concurrent_running_tasks tại
POST /settings/general (section="rollback") - xem app/routers/settings_router.py.

Yêu cầu: input âm hoặc không phải số phải trả lỗi thân thiện, KHÔNG crash 500, KHÔNG
ghi giá trị rác vào DB. Convention theo tests/test_settings_git_branch_fields.py."""

import json

from app.models import Role
from app.services.app_setting_service import get_app_setting
from tests.conftest import make_user

VALID_ROLLBACK_SECTION_PAYLOAD = {
    "section": "rollback",
    "rollback_allowed_seconds": "3600",
}


def test_update_max_concurrent_running_tasks_success_with_valid_value(client_factory, db_session):
    sa = make_user(db_session, email="sa-mcrt@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)

    resp = client.post(
        "/settings/general",
        data={**VALID_ROLLBACK_SECTION_PAYLOAD, "max_concurrent_running_tasks": "5"},
        headers={"accept": "application/json"},
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    updated = get_app_setting(db_session)
    assert updated.max_concurrent_running_tasks == 5


def test_update_max_concurrent_running_tasks_zero_means_unlimited_is_accepted(client_factory, db_session):
    sa = make_user(db_session, email="sa-mcrt-zero@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)

    resp = client.post(
        "/settings/general",
        data={**VALID_ROLLBACK_SECTION_PAYLOAD, "max_concurrent_running_tasks": "0"},
        headers={"accept": "application/json"},
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    updated = get_app_setting(db_session)
    assert updated.max_concurrent_running_tasks == 0


def test_update_max_concurrent_running_tasks_negative_rejected_friendly_no_db_change(client_factory, db_session):
    sa = make_user(db_session, email="sa-mcrt-neg@example.com", role=Role.SUPER_ADMIN)
    original = get_app_setting(db_session).max_concurrent_running_tasks
    client = client_factory(user=sa)

    resp = client.post(
        "/settings/general",
        data={**VALID_ROLLBACK_SECTION_PAYLOAD, "max_concurrent_running_tasks": "-1"},
        headers={"accept": "application/json"},
    )

    assert resp.status_code == 200, "Không được crash 500 khi nhập số âm"
    body = resp.json()
    assert body["ok"] is False
    assert body["message"], "Phải có message lỗi thân thiện"

    # DB KHONG duoc ghi gia tri rac.
    unchanged = get_app_setting(db_session)
    assert unchanged.max_concurrent_running_tasks == original


def test_update_max_concurrent_running_tasks_non_numeric_rejected_friendly_and_no_db_change(
    client_factory, db_session
):
    """Sau khi dev doi 4 field int|None sang str|None + tu parse bang _parse_optional_int
    (app/routers/settings_router.py), nhap chuoi khong phai so KHONG con tra 422 tho cua
    FastAPI nua ma tra loi than thien dang {'ok': False, 'message': ...} (giong moi truong
    hop validate thu cong khac trong file nay) - va van KHONG duoc ghi gia tri rac vao DB."""
    sa = make_user(db_session, email="sa-mcrt-nan@example.com", role=Role.SUPER_ADMIN)
    original = get_app_setting(db_session).max_concurrent_running_tasks
    client = client_factory(user=sa)

    resp = client.post(
        "/settings/general",
        data={**VALID_ROLLBACK_SECTION_PAYLOAD, "max_concurrent_running_tasks": "abc"},
        headers={"accept": "application/json"},
    )

    assert resp.status_code != 500, "Nhap chuoi khong phai so KHONG duoc lam crash 500"
    assert resp.status_code == 200, (
        "Hanh vi moi: route tu parse bang _parse_optional_int va tra ve qua _respond "
        "(200 + {'ok': False, 'message': ...}), KHONG con la 422 tho cua FastAPI Form(int) nua."
    )
    body = resp.json()
    assert body["ok"] is False
    assert "Số task tối đa chạy cùng lúc" in body["message"], "Message phai neu ro ten o bi sai"

    unchanged = get_app_setting(db_session)
    assert unchanged.max_concurrent_running_tasks == original, "DB khong duoc ghi gia tri rac"


def test_update_max_concurrent_running_tasks_non_numeric_non_ajax_redirects_with_flash(client_factory, db_session):
    """Duong form thuong (khong Accept: application/json): nhap chuoi khong phai so phai
    redirect 303 sach (khong query ?ok=/msg= tren URL) va khong ghi DB - message loi nam
    trong flash session, doc lai qua GET /settings/general."""
    sa = make_user(db_session, email="sa-mcrt-nan-noajax@example.com", role=Role.SUPER_ADMIN)
    original = get_app_setting(db_session).max_concurrent_running_tasks
    client = client_factory(user=sa)

    resp = client.post(
        "/settings/general",
        data={**VALID_ROLLBACK_SECTION_PAYLOAD, "max_concurrent_running_tasks": "abc"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings/general"
    assert "?" not in resp.headers["location"], "Redirect KHONG duoc chua ?ok=/msg= tren URL"

    unchanged = get_app_setting(db_session)
    assert unchanged.max_concurrent_running_tasks == original, "DB khong duoc ghi gia tri rac"

    # base.html render flash qua `window.showToast({{ flash_msg | tojson }}, ...)` - Jinja
    # tojson escape ky tu co dau thanh \uXXXX trong <script>, nen so sanh bang chinh
    # json.dumps(...) (giong convention cua tests/test_create_task_flash_session.py) thay
    # vi tim thang chuoi tieng Viet co dau (se khong bao gio khop).
    page = client.get("/settings/general")
    assert json.dumps("Số task tối đa chạy cùng lúc phải là số nguyên hợp lệ") in page.text


def test_general_settings_page_renders_task_label_but_keeps_rollback_section_value(client_factory, db_session):
    """Regression: card đổi nhãn hiển thị "Rollback" -> "Task" (theo yêu cầu người dùng)
    nhưng hidden input `section` PHẢI vẫn giữ nguyên giá trị "rollback" - đổi giá trị này
    sẽ làm settings_router.py không map đúng section nữa (khác câu chữ hiển thị)."""
    sa = make_user(db_session, email="sa-mcrt-label@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)

    resp = client.get("/settings/general")

    assert resp.status_code == 200
    assert '<h3 style="margin-top:0;">Task</h3>' in resp.text
    assert 'name="section" value="rollback"' in resp.text
    assert 'name="max_concurrent_running_tasks"' in resp.text
    assert 'name="rollback_allowed_seconds"' in resp.text

    # Va lai round-trip: POST voi section="rollback" van phai luu duoc binh thuong (khong
    # bi anh huong boi doi nhan hien thi).
    resp2 = client.post(
        "/settings/general",
        data={**VALID_ROLLBACK_SECTION_PAYLOAD, "max_concurrent_running_tasks": "2"},
        headers={"accept": "application/json"},
    )
    assert resp2.status_code == 200
    assert resp2.json()["ok"] is True
    assert get_app_setting(db_session).max_concurrent_running_tasks == 2
