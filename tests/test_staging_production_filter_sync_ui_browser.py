"""[TEST][Full] Kiểm thử happy-path bằng trình duyệt thật (Playwright/Chromium) cho các nhóm
thay đổi UI/JS trên Staging/Production (app/templates/staging.html,
app/templates/production.html, app/templates/groups.html, app/templates/base.html):

  a) Nút Sync đổi từ nút text sang nút tròn chỉ-icon (`.icon-btn` trong base.html) - vị trí
     góc trên-phải, ghim đúng trên mobile (flex-shrink:0 + justify-content:space-between).
  b) Filter Project/Application client-side (`#staging-filter` / `#production-filter`) -
     lọc dòng bảng theo data-project + data-app, không phân biệt hoa/thường, khớp một phần,
     hiện dòng "Không có ứng dụng nào khớp bộ lọc" khi rỗng kết quả.
  c) Filter persist qua reload (sessionStorage `staging-filter-value` /
     `production-filter-value`).
  d) Tương tác Filter x "Chọn tất cả": selectableCheckboxes() chỉ chọn dòng ĐANG HIỂN THỊ
     (style.display !== 'none'), không tự bỏ chọn dòng đã chọn trước đó dù đang bị ẩn.
  e) Checkbox "Chọn tất cả" (`#staging-select-all`/`#production-select-all`) nằm trong
     toolbar bulk (`#staging-bulk-toolbar`/`#production-bulk-toolbar`, đầu toolbar, KHÔNG
     còn ở trong `<thead>`) - chỉ hiện với Admin/Super Admin (`user.is_full_access_role`) -
     user thường (role=user, chỉ được cấp cờ can_toggle_staging/production) vẫn thấy đủ
     checkbox từng dòng + toolbar "Đã chọn N ứng dụng"/Bật/Tắt hàng loạt như cũ, chỉ mất
     riêng nút "chọn hết 1 phát", vẫn tự tick nhiều dòng thủ công rồi bulk Bật/Tắt được. Vì
     đã chuyển ra khỏi `<thead>` (bị `display:none` trên mobile theo `.responsive-table`),
     checkbox này giờ hiện được cả trên mobile lẫn desktop cho Admin+.
  f) Phân trang client-side kiểu `.pagination`/`.page-btn` (giống base.html dùng cho
     /tasks) - mặc định 100 dòng/trang, chọn được 50/100/200/300/500, Prev/Next dùng class
     CSS `disabled` (không phải thuộc tính HTML `disabled`), dãy số trang render trong
     `#staging-page-numbers`/`#production-page-numbers` - filter chạy trên TOÀN BỘ danh
     sách (không chỉ trang đang xem) rồi mới phân trang kết quả, số dòng/trang lưu
     sessionStorage qua reload.
  g) Dòng thống kê "Tổng app / Đang chạy / Pod đang chạy" ngay dưới `<h2>`.
  h) 2 nút cuộn floating (`#scroll-fab`) chỉ chèn ở /tasks, /staging, /production.

Chỉ test 2 trang Staging + Production (client-side, không gọi git/ArgoCD thật). Bỏ qua toàn
bộ module nếu môi trường chưa cài sẵn Chromium cho Playwright.
"""

from __future__ import annotations

import contextlib
import socket
import subprocess
import sys
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None
    PlaywrightError = Exception

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _chromium_available() -> bool:
    if sync_playwright is None:
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        return True
    except PlaywrightError:
        return False


pytestmark = pytest.mark.skipif(
    not _chromium_available(),
    reason="Playwright Chromium chưa cài sẵn (chạy `python -m playwright install chromium`) "
    "hoặc không thể launch trình duyệt headless trong môi trường này.",
)


# ---------------------------------------------------------------------------
# Live server fixture (giống pattern test_dmy_picker_widget_browser.py)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    work_dir = tmp_path_factory.mktemp("staging_prod_filter_live_server")
    db_path = work_dir / "filter_test.db"
    db_url = f"sqlite:///{db_path}"
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    sys.path.insert(0, str(REPO_ROOT))
    import app.models as models_module

    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    models_module.Base.metadata.create_all(engine)
    engine.dispose()

    env = {
        "PATH": __import__("os").environ.get("PATH", ""),
        "DATABASE_URL": db_url,
        "SECRET_KEY": "test-secret-key-for-staging-prod-filter-browser-test",
        "AUTH_DEV_MODE": "true",
        "APP_URL": base_url,
        "ENABLE_SCHEDULER": "false",
        "ENABLE_MAIL": "false",
        "GIT_REMOTE_CREDENTIALS": "",
    }

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    ready = False
    deadline = time.time() + 20
    last_err = None
    while time.time() < deadline:
        try:
            import urllib.request

            with urllib.request.urlopen(f"{base_url}/auth/login", timeout=1) as resp:
                if resp.status == 200:
                    ready = True
                    break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if proc.poll() is not None:
                break
            time.sleep(0.2)

    if not ready:
        out = ""
        with contextlib.suppress(Exception):
            proc.terminate()
            out = proc.communicate(timeout=5)[0]
        pytest.fail(f"Live server không khởi động được (err={last_err}).\nServer output:\n{out}")

    yield {"base_url": base_url, "db_url": db_url, "port": port}

    proc.terminate()
    with contextlib.suppress(Exception):
        proc.wait(timeout=5)
    if proc.poll() is None:
        proc.kill()


@pytest.fixture()
def db_conn(live_server):
    engine = create_engine(live_server["db_url"], connect_args={"check_same_thread": False})
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture()
def page(browser):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    pg = context.new_page()
    yield pg
    context.close()


@pytest.fixture()
def mobile_page(browser):
    context = browser.new_context(viewport={"width": 375, "height": 812})
    pg = context.new_page()
    yield pg
    context.close()


def dev_login(pg, base_url: str, email: str) -> None:
    pg.goto(f"{base_url}/auth/login")
    pg.fill('input[name="email"]', email)
    pg.click('form[action="/auth/dev-login"] button[type="submit"]')
    pg.wait_for_url(f"{base_url}/home")


def promote_to_super_admin(db_conn, email: str) -> None:
    from app.models import Role, User

    user = db_conn.query(User).filter(User.email == email).one()
    user.role = Role.SUPER_ADMIN
    db_conn.commit()


def grant_toggle_permission(db_conn, email: str, env: str) -> None:
    """Cap cong tac can_toggle_staging/can_toggle_production (KHONG doi role) - mo phong
    "user thuong duoc Super Admin cap quyen" thay vi thang len Admin/Super Admin."""
    from app.models import User

    user = db_conn.query(User).filter(User.email == email).one()
    if env == "staging":
        user.can_toggle_staging = True
    else:
        user.can_toggle_production = True
    db_conn.commit()


def seed_env_apps(db_conn, env: str, rows: list[tuple[str, str, str]]) -> None:
    """rows: list of (project, application, replicas). replicas='0' => OFF/disabled.
    Xoá sạch cache cũ của đúng `env` này trước khi seed (module-scoped live_server dùng
    chung 1 DB xuyên suốt các test được parametrize trong file - tránh đụng UNIQUE
    constraint (env, project, application) giữa các lần seed lặp lại cùng env)."""
    from app.models import EnvAppCache

    db_conn.query(EnvAppCache).filter_by(env=env).delete()
    db_conn.commit()
    for project, application, replicas in rows:
        db_conn.add(EnvAppCache(env=env, project=project, application=application, replicas=replicas))
    db_conn.commit()


SEED_ROWS = [
    ("core", "api-service", "2"),
    ("CORE", "Worker", "1"),
    ("payment", "gateway", "3"),
    ("billing", "invoice", "0"),
]


# ---------------------------------------------------------------------------
# (a) Nút Sync tròn - vị trí + pin trên mobile
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path,select_all_id", [("/staging", "staging-select-all"), ("/production", "production-select-all")])
def test_a_sync_icon_button_round_and_pinned_top_right_mobile(
    live_server, mobile_page, db_conn, path, select_all_id
):
    env = "staging" if path == "/staging" else "production"
    email = f"a-tester-{env}@example.com"
    dev_login(mobile_page, live_server["base_url"], email)
    promote_to_super_admin(db_conn, email)
    seed_env_apps(db_conn, env, SEED_ROWS)

    mobile_page.goto(f"{live_server['base_url']}{path}")
    # LUU Y: KHONG wait_for_selector(f"#{select_all_id}") o day - o mobile (<=700px),
    # table.responsive-table thead { display:none } (base.html) an luon <thead> chua
    # #staging-select-all/#production-select-all, selector nay se timeout du trang da
    # load xong (ngoai pham vi test nut Sync - chi dung de dam bao trang da render).
    mobile_page.wait_for_selector("#sync-form button.icon-btn")

    sync_form = mobile_page.locator("#sync-form")
    sync_btn = mobile_page.locator("#sync-form button.icon-btn")
    assert sync_form.count() == 1
    assert sync_btn.count() == 1

    form_box = sync_form.bounding_box()
    btn_box = sync_btn.bounding_box()
    viewport_width = 375
    assert form_box is not None and btn_box is not None

    # Ghim sát mép phải viewport (khu vực card có padding, chấp nhận sai số hợp lý).
    assert btn_box["x"] + btn_box["width"] >= viewport_width - 60, (
        f"[{env}] Nút Sync không nằm sát góc phải trên mobile: btn_box={btn_box}"
    )
    # Nút hình tròn: width == height, border-radius 50%.
    computed = sync_btn.evaluate(
        "el => { const s = getComputedStyle(el); "
        "return {width: el.getBoundingClientRect().width, height: el.getBoundingClientRect().height, "
        "borderRadius: s.borderRadius, flexShrink: getComputedStyle(el.closest('#sync-form')).flexShrink}; }"
    )
    assert abs(computed["width"] - computed["height"]) < 1.0, f"[{env}] Nút Sync không phải hình tròn: {computed}"
    assert computed["flexShrink"] == "0", f"[{env}] #sync-form thiếu flex-shrink:0, có thể bị co lại trên mobile: {computed}"


# ---------------------------------------------------------------------------
# (b) Filter client-side: khớp 1 phần, không phân biệt hoa/thường, empty-state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,filter_id,row_selector,empty_id",
    [
        ("/staging", "staging-filter", "tbody tr[data-project]", "staging-filter-empty"),
        ("/production", "production-filter", "tbody tr[data-project]", "production-filter-empty"),
    ],
)
def test_b_filter_matches_case_insensitive_partial_and_shows_empty_state(
    live_server, page, db_conn, path, filter_id, row_selector, empty_id
):
    env = "staging" if path == "/staging" else "production"
    email = f"b-tester-{env}@example.com"
    dev_login(page, live_server["base_url"], email)
    promote_to_super_admin(db_conn, email)
    seed_env_apps(db_conn, env, SEED_ROWS)

    page.goto(f"{live_server['base_url']}{path}")
    page.wait_for_selector(f"#{filter_id}")

    def visible_rows():
        rows = page.locator(row_selector)
        out = []
        for i in range(rows.count()):
            row = rows.nth(i)
            if row.evaluate("el => el.style.display !== 'none'"):
                out.append(row.get_attribute("data-project") + "/" + row.get_attribute("data-app"))
        return out

    assert len(visible_rows()) == 4, "4 dòng seed phải hiển thị đủ khi chưa lọc"

    # Khớp partial + không phân biệt hoa/thường: "cor" phải khớp cả "core" và "CORE".
    page.fill(f"#{filter_id}", "cor")
    page.wait_for_timeout(50)
    vis = visible_rows()
    assert set(vis) == {"core/api-service", "CORE/Worker"}, f"[{env}] Filter 'cor' sai kết quả: {vis}"
    assert page.locator(f"#{empty_id}").evaluate("el => el.style.display") == "none"

    # Khớp theo application field: "invoice" chỉ khớp billing/invoice.
    page.fill(f"#{filter_id}", "INVOICE")
    page.wait_for_timeout(50)
    vis = visible_rows()
    assert vis == ["billing/invoice"], f"[{env}] Filter 'INVOICE' (hoa) sai kết quả: {vis}"

    # Không khớp gì -> empty-state hiện, tất cả dòng data ẩn.
    page.fill(f"#{filter_id}", "khong-ton-tai-xyz")
    page.wait_for_timeout(50)
    assert visible_rows() == []
    assert page.locator(f"#{empty_id}").evaluate("el => el.style.display") != "none", (
        f"[{env}] Dòng 'Không có ứng dụng nào khớp bộ lọc' phải hiện khi 0 kết quả"
    )

    # Xoá filter -> về lại đủ 4 dòng, empty-state ẩn lại.
    page.fill(f"#{filter_id}", "")
    page.wait_for_timeout(50)
    assert len(visible_rows()) == 4
    assert page.locator(f"#{empty_id}").evaluate("el => el.style.display") == "none"


# ---------------------------------------------------------------------------
# (c) Filter persist qua reload (sessionStorage)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,filter_id,storage_key,row_selector",
    [
        ("/staging", "staging-filter", "staging-filter-value", "tbody tr[data-project]"),
        ("/production", "production-filter", "production-filter-value", "tbody tr[data-project]"),
    ],
)
def test_c_filter_persists_across_reload_via_sessionstorage(
    live_server, page, db_conn, path, filter_id, storage_key, row_selector
):
    env = "staging" if path == "/staging" else "production"
    email = f"c-tester-{env}@example.com"
    dev_login(page, live_server["base_url"], email)
    promote_to_super_admin(db_conn, email)
    seed_env_apps(db_conn, env, SEED_ROWS)

    page.goto(f"{live_server['base_url']}{path}")
    page.wait_for_selector(f"#{filter_id}")

    page.fill(f"#{filter_id}", "payment")
    page.wait_for_timeout(50)

    stored = page.evaluate(f"() => sessionStorage.getItem('{storage_key}')")
    assert stored == "payment", f"[{env}] sessionStorage['{storage_key}'] không lưu đúng giá trị gõ: {stored!r}"

    page.reload()
    page.wait_for_selector(f"#{filter_id}")

    restored_value = page.input_value(f"#{filter_id}")
    assert restored_value == "payment", f"[{env}] Input filter không được khôi phục sau reload: {restored_value!r}"

    rows = page.locator(row_selector)
    visible = [
        rows.nth(i).get_attribute("data-project") + "/" + rows.nth(i).get_attribute("data-app")
        for i in range(rows.count())
        if rows.nth(i).evaluate("el => el.style.display !== 'none'")
    ]
    assert visible == ["payment/gateway"], (
        f"[{env}] applyFilter() không tự chạy lại sau khi khôi phục giá trị filter từ sessionStorage: {visible}"
    )


# ---------------------------------------------------------------------------
# (d) "Chọn tất cả" chỉ chọn dòng đang hiển thị khi filter đang ẩn bớt dòng
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,filter_id,select_all_id,checkbox_class",
    [
        ("/staging", "staging-filter", "staging-select-all", "staging-select"),
        ("/production", "production-filter", "production-select-all", "production-select"),
    ],
)
def test_d_select_all_only_selects_currently_visible_rows(
    live_server, page, db_conn, path, filter_id, select_all_id, checkbox_class
):
    env = "staging" if path == "/staging" else "production"
    email = f"d-tester-{env}@example.com"
    dev_login(page, live_server["base_url"], email)
    promote_to_super_admin(db_conn, email)
    seed_env_apps(db_conn, env, SEED_ROWS)

    page.goto(f"{live_server['base_url']}{path}")
    page.wait_for_selector(f"#{select_all_id}")

    # Loc con 2/4 dong (project chua "cor": core/api-service, CORE/Worker).
    page.fill(f"#{filter_id}", "cor")
    page.wait_for_timeout(50)

    page.click(f"#{select_all_id}")

    def checked_values():
        boxes = page.locator(f".{checkbox_class}")
        return [
            boxes.nth(i).get_attribute("value")
            for i in range(boxes.count())
            if boxes.nth(i).is_checked()
        ]

    checked = checked_values()
    assert set(checked) == {"core|api-service", "CORE|Worker"}, (
        f"[{env}] 'Chọn tất cả' phải chỉ chọn 2 dòng đang hiển thị (filter='cor'), thực tế: {checked}"
    )

    # Xoa filter: 2 dong con lai (payment/gateway, billing/invoice) KHONG bi tu dong chon
    # theo (chi dong dang hien tai thoi diem bam Chon tat ca moi duoc chon).
    page.fill(f"#{filter_id}", "")
    page.wait_for_timeout(50)
    checked_after_clear = checked_values()
    assert set(checked_after_clear) == {"core|api-service", "CORE|Worker"}, (
        f"[{env}] Trạng thái chọn phải giữ nguyên sau khi xoá filter (không tự chọn/bỏ chọn "
        f"dòng vừa hiện lại), thực tế: {checked_after_clear}"
    )

    # Dem hien thi (#..-bulk-count) phai phan anh TONG so da chon (khong chi dong dang hien).
    count_prefix = "staging" if env == "staging" else "production"
    count_text = page.locator(f"#{count_prefix}-bulk-count").inner_text()
    assert "2" in count_text, f"[{env}] Bộ đếm 'Đã chọn N ứng dụng' sai: {count_text!r}"


# ---------------------------------------------------------------------------
# (e) "Chọn tất cả" chỉ hiện với Admin/Super Admin - user thường chỉ check từng dòng
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,select_all_id,checkbox_class",
    [
        ("/staging", "staging-select-all", "staging-select"),
        ("/production", "production-select-all", "production-select"),
    ],
)
def test_e_select_all_checkbox_only_visible_for_admin_role(
    live_server, page, db_conn, path, select_all_id, checkbox_class
):
    env = "staging" if path == "/staging" else "production"
    email = f"e-tester-{env}@example.com"
    dev_login(page, live_server["base_url"], email)
    grant_toggle_permission(db_conn, email, env)  # role=USER, chi duoc cap cong tac rieng
    seed_env_apps(db_conn, env, SEED_ROWS)

    page.goto(f"{live_server['base_url']}{path}")
    page.wait_for_selector(f".{checkbox_class}")

    assert page.locator(f"#{select_all_id}").count() == 0, (
        f"[{env}] User thường (role=user, chỉ có cờ can_toggle_*) không được thấy checkbox "
        f"'Chọn tất cả'"
    )
    assert page.locator(f".{checkbox_class}").count() == 4, (
        f"[{env}] User thường vẫn phải check được từng dòng riêng lẻ (chỉ mất nút "
        f"'chọn hết 1 phát', KHÔNG mất cả cột checkbox lẫn toolbar bulk Bật/Tắt)"
    )

    from app.models import Role, User

    user = db_conn.query(User).filter(User.email == email).one()
    user.role = Role.ADMIN
    db_conn.commit()

    page.goto(f"{live_server['base_url']}{path}")
    page.wait_for_selector(f"#{select_all_id}")
    assert page.locator(f"#{select_all_id}").count() == 1, (
        f"[{env}] Sau khi lên Admin, checkbox 'Chọn tất cả' phải xuất hiện"
    )


# ---------------------------------------------------------------------------
# (f) Phân trang: mặc định 50 dòng/trang, filter tìm xuyên qua các trang khác
# ---------------------------------------------------------------------------


PAGINATION_SEED_ROWS = [(f"proj{i:03d}", f"app{i:03d}", "1") for i in range(150)]


@pytest.mark.parametrize(
    "path,filter_id,page_size_id,pagination_id,page_numbers_id,prev_id,next_id",
    [
        (
            "/staging", "staging-filter", "staging-page-size", "staging-pagination",
            "staging-page-numbers", "staging-page-prev", "staging-page-next",
        ),
        (
            "/production", "production-filter", "production-page-size", "production-pagination",
            "production-page-numbers", "production-page-prev", "production-page-next",
        ),
    ],
)
def test_f_pagination_default_50_and_filter_finds_rows_on_other_pages(
    live_server, page, db_conn, path, filter_id, page_size_id, pagination_id, page_numbers_id, prev_id, next_id
):
    env = "staging" if path == "/staging" else "production"
    email = f"f-tester-{env}@example.com"
    dev_login(page, live_server["base_url"], email)
    promote_to_super_admin(db_conn, email)
    seed_env_apps(db_conn, env, PAGINATION_SEED_ROWS)

    page.goto(f"{live_server['base_url']}{path}")
    page.wait_for_selector(f"#{pagination_id}")

    def visible_apps():
        rows = page.locator("tbody tr[data-project]")
        return [
            rows.nth(i).get_attribute("data-app")
            for i in range(rows.count())
            if rows.nth(i).evaluate("el => el.style.display !== 'none'")
        ]

    def has_disabled_class(locator_id):
        # UI moi dung CSS class 'disabled' (JS classList.toggle) thay vi thuoc tinh HTML
        # 'disabled' - is_disabled() cua Playwright se luon tra ve False cho <button> thuong
        # dung o day, phai kiem tra qua classList.
        return page.locator(f"#{locator_id}").evaluate("el => el.classList.contains('disabled')")

    assert page.input_value(f"#{page_size_id}") == "50", f"[{env}] Mặc định số dòng/trang phải là 50"
    assert len(visible_apps()) == 50, f"[{env}] Trang 1 (page size 50) phải hiện đúng 50 dòng"

    page_number_btns = page.locator(f"#{page_numbers_id} .page-btn")
    assert page_number_btns.count() == 3, (
        f"[{env}] 150 dòng / 50 mỗi trang phải sinh đúng 3 nút số trang (1, 2 và 3)"
    )
    assert page_number_btns.nth(0).get_attribute("data-page") == "1"
    assert page_number_btns.nth(1).get_attribute("data-page") == "2"
    assert page_number_btns.nth(2).get_attribute("data-page") == "3"
    assert "active" in (page_number_btns.nth(0).get_attribute("class") or ""), (
        f"[{env}] Nút số trang 1 phải có class 'active' khi đang ở trang 1"
    )

    assert has_disabled_class(prev_id), f"[{env}] Prev phải có class 'disabled' ở trang 1"
    assert not has_disabled_class(next_id), f"[{env}] Next KHÔNG được có class 'disabled' ở trang 1"

    page.click(f"#{next_id}")
    page.wait_for_timeout(50)
    assert len(visible_apps()) == 50, f"[{env}] Trang 2 phải còn lại đúng 50 dòng (150 - 50)"
    assert not has_disabled_class(next_id), (
        f"[{env}] Next KHÔNG được có class 'disabled' ở trang 2 (còn trang 3 phía sau)"
    )
    page_number_btns = page.locator(f"#{page_numbers_id} .page-btn")
    assert "active" in (page_number_btns.nth(1).get_attribute("class") or ""), (
        f"[{env}] Nút số trang 2 phải có class 'active' sau khi bấm Next"
    )

    page.click(f"#{next_id}")
    page.wait_for_timeout(50)
    assert len(visible_apps()) == 50, f"[{env}] Trang 3 phải còn lại đúng 50 dòng (150 - 50 - 50)"
    assert has_disabled_class(next_id), f"[{env}] Next phải có class 'disabled' ở trang cuối (trang 3)"
    page_number_btns = page.locator(f"#{page_numbers_id} .page-btn")
    assert "active" in (page_number_btns.nth(2).get_attribute("class") or ""), (
        f"[{env}] Nút số trang 3 phải có class 'active' sau khi bấm Next lần 2"
    )

    # Filter phai chay tren TOAN BO 150 dong (khong chi trang dang xem) - dang dung o
    # trang 3 (app100-app149) nhung go ten 1 dong nam o trang 2 (app050-app099) van phai
    # tim thay, khong bi coi la "khong ton tai" chi vi dang dung sai trang.
    page.fill(f"#{filter_id}", "app099")
    page.wait_for_timeout(50)
    vis = visible_apps()
    assert vis == ["app099"], f"[{env}] Filter phải tìm thấy dòng khớp dù đang đứng ở trang khác: {vis}"

    page.fill(f"#{filter_id}", "")
    page.wait_for_timeout(50)

    # Doi so dong/trang len 200 -> du 150 dong deu vua 1 trang, thanh phan trang phai an di.
    page.select_option(f"#{page_size_id}", "200")
    page.wait_for_timeout(50)
    assert len(visible_apps()) == 150
    assert page.locator(f"#{pagination_id}").evaluate("el => getComputedStyle(el).display") == "none"

    # Doi so dong/trang duoc luu sessionStorage, khoi phuc dung sau reload.
    stored = page.evaluate(f"() => sessionStorage.getItem('{env}-page-size-value')")
    assert stored == "200", f"[{env}] Số dòng/trang phải được lưu sessionStorage: {stored!r}"
    page.reload()
    # state="attached" (khong doi "visible" mac dinh): #..-pagination dang AN (150 dong
    # <= 200/trang => 1 trang => display:none theo thiet ke), cho "visible" se timeout oan
    # du hanh vi dung.
    page.wait_for_selector(f"#{page_size_id}", state="attached")
    assert page.input_value(f"#{page_size_id}") == "200", f"[{env}] Số dòng/trang phải khôi phục sau reload"


# ---------------------------------------------------------------------------
# (g) Dòng thống kê "Tổng app / Đang chạy / Pod đang chạy" dưới <h2>
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/staging", "/production"])
def test_g_stat_line_shows_total_running_apps_and_pods(live_server, page, db_conn, path):
    env = "staging" if path == "/staging" else "production"
    email = f"g-tester-{env}@example.com"
    dev_login(page, live_server["base_url"], email)
    promote_to_super_admin(db_conn, email)
    seed_env_apps(db_conn, env, SEED_ROWS)

    page.goto(f"{live_server['base_url']}{path}")
    page.wait_for_selector("h2")

    # SEED_ROWS: 4 dong (replicas 2,1,3,0) => 3 dong ON, tong 2+1+3=6 pods.
    body_text = page.inner_text("body")
    assert "Tổng app: 4" in body_text, f"[{env}] Thiếu/sai tổng số ứng dụng trong dòng thống kê"
    assert "Đang chạy: 3" in body_text, f"[{env}] Thiếu/sai số ứng dụng đang chạy trong dòng thống kê"
    assert "Pod đang chạy: 6" in body_text, f"[{env}] Thiếu/sai tổng pods đang chạy trong dòng thống kê"


# ---------------------------------------------------------------------------
# (h) Checkbox "Chọn tất cả" hiện được trên MOBILE cho Admin+, ẩn với user thường
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,select_all_id,checkbox_class",
    [
        ("/staging", "staging-select-all", "staging-select"),
        ("/production", "production-select-all", "production-select"),
    ],
)
def test_h_select_all_checkbox_visible_on_mobile_for_admin_hidden_for_user(
    live_server, mobile_page, db_conn, path, select_all_id, checkbox_class
):
    env = "staging" if path == "/staging" else "production"

    # Admin/Super Admin tren mobile: checkbox "Chon tat ca" phai HIEN duoc (truoc day nam
    # trong <thead> bi table.responsive-table thead { display:none } o mobile che mat).
    admin_email = f"h-admin-{env}@example.com"
    dev_login(mobile_page, live_server["base_url"], admin_email)
    promote_to_super_admin(db_conn, admin_email)
    seed_env_apps(db_conn, env, SEED_ROWS)

    mobile_page.goto(f"{live_server['base_url']}{path}")
    mobile_page.wait_for_selector(f"#{select_all_id}")
    assert mobile_page.locator(f"#{select_all_id}").is_visible(), (
        f"[{env}] Checkbox 'Chọn tất cả' phải HIỂN THỊ trên mobile cho Admin/Super Admin "
        f"(đã chuyển ra khỏi <thead> sang đầu toolbar bulk)"
    )

    # User thuong (chi cap co can_toggle_staging/production, khong len role Admin) tren
    # mobile: KHONG thay checkbox "Chon tat ca" nhung van thay du checkbox tung dong.
    # Phai /auth/logout truoc: da dang nhap roi thi GET /auth/login se redirect thang ve
    # "/" (xem auth_router.login_page), khong con form email de dev_login() dien lai.
    mobile_page.goto(f"{live_server['base_url']}/auth/logout")
    user_email = f"h-user-{env}@example.com"
    dev_login(mobile_page, live_server["base_url"], user_email)
    grant_toggle_permission(db_conn, user_email, env)

    mobile_page.goto(f"{live_server['base_url']}{path}")
    mobile_page.wait_for_selector(f".{checkbox_class}")
    assert mobile_page.locator(f"#{select_all_id}").count() == 0, (
        f"[{env}] User thường (role=user) không được thấy checkbox 'Chọn tất cả' trên mobile"
    )
    assert mobile_page.locator(f".{checkbox_class}").count() == 4, (
        f"[{env}] User thường vẫn phải check được từng dòng riêng lẻ trên mobile"
    )


# ---------------------------------------------------------------------------
# (i) Nút cuộn floating (#scroll-fab) chỉ chèn ở /tasks, /staging, /production
# ---------------------------------------------------------------------------


def test_i_scroll_fab_present_only_on_gated_routes(live_server, page, db_conn):
    email = "i-tester@example.com"
    dev_login(page, live_server["base_url"], email)
    promote_to_super_admin(db_conn, email)
    seed_env_apps(db_conn, "staging", SEED_ROWS)
    seed_env_apps(db_conn, "production", SEED_ROWS)

    for path in ("/staging", "/production", "/tasks"):
        page.goto(f"{live_server['base_url']}{path}")
        page.wait_for_selector("#scroll-fab", state="attached")
        fab = page.locator("#scroll-fab")
        assert fab.count() >= 1, f"[{path}] Thiếu #scroll-fab"
        buttons = fab.locator("button")
        assert buttons.count() == 2, f"[{path}] #scroll-fab phải chứa đúng 2 nút bấm (lên đầu/xuống cuối)"

    # /home khong thuoc nhom duoc gate -> khong duoc chen #scroll-fab.
    page.goto(f"{live_server['base_url']}/home")
    assert page.locator("#scroll-fab").count() == 0, "/home không được chèn #scroll-fab"
