"""Kiểm thử fix UI: checkbox multi-select ở màn "Danh sách" (task_list.html) hiển thị
màu đen thay vì theo theme sáng của app. Bản sửa chỉ đụng app/templates/base.html:
  1. Đổi `:root { color-scheme: light dark; }` -> `:root { color-scheme: light; }`
  2. Thêm rule `input[type="checkbox"]` (accent-color, color-scheme, cursor, size) +
     `input[type="checkbox"]:disabled` (cursor, opacity).

Đây là lỗi thuần CSS/hiển thị, không có logic backend mới - test tập trung vào:
  - Nội dung/cú pháp CSS trong base.html đúng như mô tả, không hồi quy
    (color-scheme không còn "light dark", brace cân bằng).
  - task_list.html vẫn render đúng, checkbox #select-all/.task-select vẫn có mặt và
    JS chọn dòng không bị ảnh hưởng (route /tasks vẫn trả 200 và HTML đúng cấu trúc).
  - Checkbox khác dùng chung rule global (login "remember", settings_general
    enable_mail/enable_scheduler/enable_30min_reminder) không bị phá vỡ.
"""

import re
from pathlib import Path

from app.models import DeployTask, Role, TaskStatus
from tests.conftest import make_user

BASE_HTML = Path(__file__).resolve().parent.parent / "app" / "templates" / "base.html"
TASK_LIST_HTML = Path(__file__).resolve().parent.parent / "app" / "templates" / "task_list.html"


def _style_block(html_text: str) -> str:
    match = re.search(r"<style>(.*?)</style>", html_text, re.S)
    assert match, "base.html phải có block <style>...</style>"
    return match.group(1)


def make_task(db_session, status=TaskStatus.TASK, **kwargs):
    defaults = dict(
        project="core",
        application="api",
        commitid="abc1234",
        status=status,
        email="owner@example.com",
    )
    defaults.update(kwargs)
    task = DeployTask(**defaults)
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


# ---------------------------------------------------------------------------
# 1) base.html: nội dung/cú pháp CSS
# ---------------------------------------------------------------------------


def test_root_color_scheme_locked_to_light_only():
    html_text = BASE_HTML.read_text(encoding="utf-8")
    style = _style_block(html_text)

    assert "color-scheme: light dark" not in style, (
        "Regression: ':root { color-scheme: light dark; }' vẫn còn - checkbox (và các "
        "native control khác) sẽ lại render theo dark mode của OS."
    )
    assert re.search(r":root\s*\{\s*color-scheme:\s*light\s*;\s*\}", style), (
        "Không tìm thấy rule ':root { color-scheme: light; }' đúng như bản sửa mô tả."
    )


def test_checkbox_rule_present_with_expected_declarations():
    """accent-color de trinh duyet tu ve dau tick da bi bo (moi trinh duyet/nen tang tu
    chon mau tuong phan rieng - PC ra tick den, mobile ra tick trang, lech giao dien).
    Border-trick (::after + rotate(45deg)) sau do cung bi bo - toa do px le (4.8/2.8/...)
    bi cac trinh duyet/thiet bi lam tron subpixel khac nhau nen PC/mobile van lech nhe.
    Base.html gio ve dau tick bang SVG (background-image, vector) - render giong het nhau
    moi noi, khong con phu thuoc lam tron subpixel."""
    html_text = BASE_HTML.read_text(encoding="utf-8")
    style = _style_block(html_text)

    rule_match = re.search(r'input\[type="checkbox"\]\s*\{([^}]*)\}', style)
    assert rule_match, 'Thiếu rule input[type="checkbox"] { ... } trong base.html.'
    rule_body = rule_match.group(1)

    assert "appearance: none" in rule_body
    assert "cursor: pointer" in rule_body
    assert "width: 16px" in rule_body
    assert "height: 16px" in rule_body
    assert "vertical-align: middle" in rule_body

    checked_match = re.search(r'input\[type="checkbox"\]:checked\s*\{([^}]*)\}', style)
    assert checked_match, 'Thiếu rule input[type="checkbox"]:checked { ... } trong base.html.'
    checked_body = checked_match.group(1)
    assert "#3699FF" in checked_body
    assert "background-image: url(" in checked_body, (
        "Dau tick phai ve bang SVG (background-image) thay vi border-trick de tranh lech "
        "subpixel giua cac trinh duyet/thiet bi."
    )
    assert "svg+xml" in checked_body and "stroke='white'" in checked_body


def test_checkbox_disabled_rule_present():
    html_text = BASE_HTML.read_text(encoding="utf-8")
    style = _style_block(html_text)

    disabled_match = re.search(r'input\[type="checkbox"\]:disabled\s*\{([^}]*)\}', style)
    assert disabled_match, 'Thiếu rule input[type="checkbox"]:disabled { ... } trong base.html.'
    disabled_body = disabled_match.group(1)

    assert "cursor: not-allowed" in disabled_body
    assert "opacity: 0.45" in disabled_body


def test_style_block_braces_are_balanced():
    """Guard chống cú pháp CSS bị hỏng (thiếu dấu { hoặc }) - nếu lệch, toàn bộ rule
    phía sau chỗ hỏng có thể bị trình duyệt bỏ qua hoặc hiểu sai."""
    html_text = BASE_HTML.read_text(encoding="utf-8")
    style = _style_block(html_text)

    assert style.count("{") == style.count("}")


def test_checkbox_rule_appears_after_input_focus_rules_not_overridden():
    """Rule input[type="checkbox"] phải đứng SAU các rule input[type="text"] chung
    chung (nếu selector cùng độ đặc hiệu, thứ tự sau thắng) - nếu vô tình đặt
    trước/bị override bởi rule chung `input, select, textarea, button { ... }` phía
    trên thì fix coi như không có tác dụng thực tế."""
    html_text = BASE_HTML.read_text(encoding="utf-8")
    style = _style_block(html_text)

    generic_input_pos = style.index("input, select, textarea, button")
    checkbox_rule_pos = style.index('input[type="checkbox"] {')
    assert checkbox_rule_pos > generic_input_pos


# ---------------------------------------------------------------------------
# 2) task_list.html: markup/JS không bị đụng, checkbox vẫn render đúng cấu trúc
# ---------------------------------------------------------------------------


def test_task_list_template_untouched_markup_anchors_present():
    """Xác nhận các anchor JS đang bám vào (#select-all, .task-select, hàm
    checkboxes()/selectableCheckboxes()/selectedIds()/refresh()) vẫn còn nguyên -
    bản sửa CSS không được đụng tới file này."""
    html_text = TASK_LIST_HTML.read_text(encoding="utf-8")

    assert 'id="select-all"' in html_text
    assert 'class="task-select"' in html_text
    assert "{% if not can_select %}disabled{% endif %}" in html_text
    for fn in ["function checkboxes()", "function selectableCheckboxes()", "function selectedIds()", "function refresh()"]:
        assert fn in html_text
    assert "selectAll.addEventListener('change'" in html_text
    assert "cb.addEventListener('change', refresh)" in html_text
    # Giới hạn thiết kế hiện tại (không phải lỗi mới): không set thuộc tính
    # `indeterminate` cho #select-all khi chỉ chọn 1 phần task.
    assert "indeterminate" not in html_text


def test_tasks_list_renders_checkboxes_end_to_end(client_factory, db_session):
    """Smoke test tích hợp: route /tasks vẫn render 200 và HTML thật sự chứa
    checkbox #select-all + .task-select sau khi sửa base.html (đảm bảo style mới
    không làm gãy Jinja block content / render pipeline)."""
    owner = make_user(db_session, email="owner@example.com", role=Role.USER)
    make_task(db_session, email=owner.email, commitid="ccc3333", status=TaskStatus.TASK)

    client = client_factory(user=owner)
    resp = client.get("/tasks")

    assert resp.status_code == 200
    assert 'id="select-all"' in resp.text
    assert 'class="task-select" value=' in resp.text
    assert "ccc3333" in resp.text


def test_tasks_list_terminal_task_checkbox_disabled(client_factory, db_session):
    """Task ở trạng thái Done (terminal) -> can_select luôn False bất kể quyền ->
    checkbox .task-select tương ứng phải có thuộc tính disabled."""
    owner = make_user(db_session, email="owner@example.com", role=Role.USER)
    make_task(db_session, email=owner.email, commitid="ddd4444", status=TaskStatus.DONE)

    client = client_factory(user=owner)
    resp = client.get("/tasks")

    assert resp.status_code == 200
    row_match = re.search(
        r'<input type="checkbox" class="task-select" value="\d+"[^>]*>', resp.text
    )
    assert row_match, "Không tìm thấy checkbox .task-select trong HTML trả về."
    assert "disabled" in row_match.group(0)


# ---------------------------------------------------------------------------
# 3) Checkbox khác dùng chung rule global - không bị hồi quy
# ---------------------------------------------------------------------------


def test_login_page_remember_checkbox_still_renders(client_factory, monkeypatch):
    """Checkbox #remember ở login.html dùng chung rule global input[type=checkbox] -
    xác nhận trang login vẫn render OK, không có checkbox nào hỏng do rule mới.
    (auth_enable_password mặc định False trong test env -> phải bật tạm để form
    email/password + checkbox #remember thực sự được render.)"""
    import app.routers.auth_router as auth_router_module

    monkeypatch.setattr(auth_router_module.settings, "auth_enable_password", True)

    client = client_factory(user=None)
    resp = client.get("/auth/login")

    assert resp.status_code == 200
    assert 'id="remember"' in resp.text
    assert 'type="checkbox"' in resp.text


def test_auto_deploy_config_confirm_checkbox_uses_global_rule(client_factory, db_session):
    """#auto-deploy-config-confirm (modal auto-deploy trong task_list.html) không có
    CSS riêng đè lên - phải thừa hưởng đúng rule global mới (accent-color/size), không
    bị rule nào khác trong task_list.html/base.html ghi đè ngược lại màu đen."""
    owner = make_user(db_session, email="owner@example.com", role=Role.USER)
    make_task(db_session, email=owner.email, commitid="eee5555")

    client = client_factory(user=owner)
    resp = client.get("/tasks")

    assert resp.status_code == 200
    assert 'id="auto-deploy-config-confirm"' in resp.text

    html_text = TASK_LIST_HTML.read_text(encoding="utf-8")
    # Không có style riêng nào set accent-color/color-scheme trong task_list.html
    # có thể override rule global của base.html.
    assert "accent-color" not in html_text
    assert "color-scheme" not in html_text
