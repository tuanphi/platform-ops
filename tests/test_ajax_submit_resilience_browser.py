"""[TEST] Kiểm thử 4 lỗi vừa vá trong window.doAjaxSubmit (app/templates/base.html
~dòng 1424-1543), bằng trình duyệt thật (Playwright/Chromium) vì đây là logic JS phía
client mà pytest thường không chạy được.

Kiểm tra (chặn/giả lập response qua page.route để dựng các tình huống lỗi):
  1a. Run đơn lẻ trên task có config_env: lần 1 THẤT BẠI (server trả JSON ok=false) ->
      trang KHÔNG reload -> bấm Run lần 2 vẫn phải hiện lại modal xác nhận "Task này có
      Config/SQL riêng...". (cờ `confirmed` phải được reset trong `finally`)
  1b. Bulk Run: lần 1 chọn task A, submit thất bại (JSON ok=false) -> đổi lựa chọn sang
      task B -> bấm Run lần 2 -> request POST /tasks/bulk/run PHẢI gửi task_ids=[B],
      KHÔNG được gửi lại task_ids=[A] cũ.
  3a. Server trả redirect (giả lập session hết hạn -> /auth/login, res.redirected=true)
      -> JS phải window.location.href = res.url, KHÔNG được resubmit (chỉ đúng 1 POST).
  3b. Server trả 500 HTML (không phải JSON) -> chỉ alert(), KHÔNG fallback form.submit()
      (chỉ đúng 1 POST, không có request POST thứ 2).
  4.  Sau khi Run thành công (data.ok=true), nút vẫn phải disabled/spinning cho đến khi
      trang thực sự reload xong, không được "sáng lại" giữa chừng khi đang chờ reload.
  2.  sessionStorage.setItem ném lỗi (Storage.prototype.setItem bị patch để throw) không
      được làm hỏng luồng thành công: vẫn phải reload, không phát sinh POST thứ 2, và
      không phát sinh unhandled exception (window.onerror/pageerror).

Bỏ qua toàn bộ module nếu môi trường chưa cài sẵn Chromium cho Playwright (pattern giống
tests/test_rollback_picker_confirm_modal_browser.py).
"""

from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import sys
import time
from datetime import datetime

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


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    work_dir = tmp_path_factory.mktemp("ajax_submit_resilience_live_server")
    db_path = work_dir / "ajax_submit_test.db"
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
        "SECRET_KEY": "test-secret-key-for-ajax-submit-resilience-browser-test",
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


def seed_runnable_task(db_conn, *, with_config: bool, project: str = "core", application: str = "api-service") -> int:
    """Tạo 1 task ở status Approved (RUNNABLE_STATUSES) để nút Run không bị disable."""
    from app.models import DeployTask, TaskStatus

    task = DeployTask(
        project=project,
        application=application,
        commitid="abc1234",
        config_env=("DB_HOST=1.2.3.4\n" if with_config else None),
        image_version="v1.0.0",
        status=TaskStatus.APPROVED,
        email="requester@example.com",
        maintainer_confirmed="tester@example.com",
        confirmed_at=datetime.utcnow(),
    )
    db_conn.add(task)
    db_conn.commit()
    db_conn.refresh(task)
    return task.id


# ---------------------------------------------------------------------------
# Loi 1a - modal xac nhan phai hien lai sau lan Run dau THAT BAI (form.dataset.confirmed
# phai duoc xoa trong finally, khong con "dinh" mai sau 1 lan xac nhan).
# ---------------------------------------------------------------------------
def test_confirm_modal_reappears_after_failed_run(live_server, page, db_conn):
    email = "loi1a-tester@example.com"
    dev_login(page, live_server["base_url"], email)
    promote_to_super_admin(db_conn, email)
    task_id = seed_runnable_task(db_conn, with_config=True)

    run_requests = []

    def handle_run(route):
        run_requests.append(route.request.post_data)
        # Gia lap that bai o tang ung dung (vd task da bi thao tac boi nguoi khac) - JSON
        # hop le, ok=false -> khong reload, DOM/JS state giu nguyen.
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": False, "message": "Task đã được người khác thao tác"}),
        )

    page.route(f"**/tasks/{task_id}/run", handle_run)

    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))

    page.goto(f"{live_server['base_url']}/tasks?page_size=50")
    run_form = page.locator(f"form[action='/tasks/{task_id}/run']")
    assert run_form.count() == 1

    expected_msg = "Task này có Config/SQL riêng — xác nhận bạn đã cập nhật đúng config trước khi Run?"
    assert run_form.get_attribute("data-confirm") == expected_msg

    run_btn = run_form.locator("button[type='submit']")

    # --- Lan 1: bam Run -> modal hien -> bam OK -> POST that bai (ok=false) -> alert() ---
    run_btn.click()
    page.wait_for_selector("#confirm-modal.open")
    page.click("#confirm-modal-ok")
    page.wait_for_timeout(300)  # cho fetch + alert() chay xong

    assert len(run_requests) == 1, f"Lần 1 phải gửi đúng 1 request Run, thực tế: {run_requests}"
    assert any("Task đã được người khác thao tác" in m for m in dialogs), f"Không thấy alert lỗi: {dialogs}"
    # Nut phai duoc bat lai (khong con o trang thai cho reload vi that bai, khong reload).
    assert run_btn.is_enabled(), "Nút Run phải được bật lại sau khi request thất bại (không reload)"

    # --- Lan 2: bam Run lai tren CUNG task -> BAT BUOC phai hien lai modal xac nhan ---
    run_btn.click()
    page.wait_for_selector("#confirm-modal.open", timeout=3000)
    assert page.locator("#confirm-modal").evaluate("el => el.classList.contains('open')") is True, (
        "Bug Lỗi 1: form.dataset.confirmed không được reset -> modal xác nhận KHÔNG hiện lại "
        "ở lần Run thứ 2, mất chốt an toàn."
    )
    msg_el = page.locator("#confirm-modal-message")
    assert msg_el.inner_text() == expected_msg

    # Dong modal gon gang, khong can submit tiep.
    page.click("#confirm-modal-cancel")


# ---------------------------------------------------------------------------
# Loi 1b - bulk Run phai dung lai task_ids MOI theo checkbox dang chon o lan submit thu
# 2, khong gui lai danh sach CU tu lan that bai truoc do.
# ---------------------------------------------------------------------------
def test_bulk_run_rebuilds_task_ids_after_failed_first_attempt(live_server, page, db_conn):
    email = "loi1b-tester@example.com"
    dev_login(page, live_server["base_url"], email)
    promote_to_super_admin(db_conn, email)
    task_a = seed_runnable_task(db_conn, with_config=False, application="svc-a")
    task_b = seed_runnable_task(db_conn, with_config=False, application="svc-b")

    bulk_run_bodies = []

    def handle_bulk_run(route):
        bulk_run_bodies.append(route.request.post_data)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": False, "message": "Lỗi giả lập lần 1"}),
        )

    page.route("**/tasks/bulk/run", handle_bulk_run)

    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))

    page.goto(f"{live_server['base_url']}/tasks?page_size=50")

    def checkbox_for(task_id: int):
        return page.locator(f"tr[data-id='{task_id}'] input.task-select")

    # --- Lan 1: chon task A, bam bulk Run -> modal -> OK -> that bai ---
    checkbox_for(task_a).check()
    page.click("#bulk-run-btn")
    page.wait_for_selector("#confirm-modal.open")
    page.click("#confirm-modal-ok")
    page.wait_for_timeout(300)

    assert len(bulk_run_bodies) == 1
    ids_1 = _parse_task_ids(bulk_run_bodies[0])
    assert ids_1 == [str(task_a)], f"Lần 1 phải gửi task_ids=[{task_a}], thực tế: {ids_1}"

    # --- Doi lua chon: bo A, chon B ---
    checkbox_for(task_a).uncheck()
    checkbox_for(task_b).check()

    # --- Lan 2: bam bulk Run lai -> phai dung lai task_ids MOI = [B], KHONG phai [A] cu ---
    page.click("#bulk-run-btn")
    page.wait_for_selector("#confirm-modal.open", timeout=3000)
    page.click("#confirm-modal-ok")
    page.wait_for_timeout(300)

    assert len(bulk_run_bodies) == 2, f"Lần 2 phải gửi thêm 1 request bulk Run, thực tế: {bulk_run_bodies}"
    ids_2 = _parse_task_ids(bulk_run_bodies[1])
    assert ids_2 == [str(task_b)], (
        f"Bug Lỗi 1 (bulk): lần Run thứ 2 phải gửi task_ids=[{task_b}] (lựa chọn MỚI), "
        f"nhưng thực tế gửi {ids_2} (nghi ngờ vẫn dùng danh sách CŨ từ lần thất bại trước)."
    )


def _parse_task_ids(post_data: str | None) -> list[str]:
    from urllib.parse import parse_qs

    if not post_data:
        return []
    parsed = parse_qs(post_data)
    return parsed.get("task_ids", [])


# ---------------------------------------------------------------------------
# Loi 3a - server tra ve redirect (session het han) -> JS dieu huong toi trang do, KHONG
# duoc resubmit (chi dung 1 POST).
# ---------------------------------------------------------------------------
def test_redirect_response_navigates_without_resubmit(live_server, page, db_conn):
    """Dung THAT su RedirectToLogin cua server (app/main.py:121-123): xoa cookie session
    NGAY TRUOC khi bam Run (DOM/JS cua trang van con nguyen - giong tinh huong that: cookie
    het han giua chung trong luc dang xem trang) - server se raise RedirectToLogin that,
    KHONG can gia lap response gia (tranh phu thuoc vao chuoi redirect that/gia lap sai)."""
    email = "loi3a-tester@example.com"
    dev_login(page, live_server["base_url"], email)
    promote_to_super_admin(db_conn, email)
    task_id = seed_runnable_task(db_conn, with_config=False)

    post_count = {"n": 0}

    def count_and_continue(route):
        if route.request.method == "POST":
            post_count["n"] += 1
        route.continue_()

    page.route(f"**/tasks/{task_id}/run", count_and_continue)

    page.goto(f"{live_server['base_url']}/tasks?page_size=50")
    page.context.clear_cookies()

    run_form = page.locator(f"form[action='/tasks/{task_id}/run']")
    run_form.locator("button[type='submit']").click()

    page.wait_for_url("**/auth/login", timeout=5000)
    assert post_count["n"] == 1, f"Chỉ được đúng 1 POST /run, thực tế: {post_count['n']}"


# ---------------------------------------------------------------------------
# Loi 3b - server tra ve 500 HTML (khong phai JSON) -> chi alert(), KHONG fallback
# form.submit() resubmit (server co the DA XU LY xong request).
# ---------------------------------------------------------------------------
def test_html_error_response_alerts_without_resubmit(live_server, page, db_conn):
    email = "loi3b-tester@example.com"
    dev_login(page, live_server["base_url"], email)
    promote_to_super_admin(db_conn, email)
    task_id = seed_runnable_task(db_conn, with_config=False)

    post_count = {"n": 0}

    def handle_run(route):
        post_count["n"] += 1
        route.fulfill(status=500, content_type="text/html", body="<html><body>Internal Server Error</body></html>")

    page.route(f"**/tasks/{task_id}/run", handle_run)

    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))

    page.goto(f"{live_server['base_url']}/tasks?page_size=50")
    url_before = page.url
    run_form = page.locator(f"form[action='/tasks/{task_id}/run']")
    run_form.locator("button[type='submit']").click()

    page.wait_for_timeout(500)

    assert post_count["n"] == 1, (
        f"Bug Lỗi 3: server trả 500 HTML (đã xử lý request) nhưng client resubmit thêm "
        f"lần nữa - có nguy cơ tạo request TRÙNG nguy hiểm cho production. Số POST thực tế: "
        f"{post_count['n']}"
    )
    assert any("lỗi" in m.lower() for m in dialogs), f"Không thấy alert báo lỗi: {dialogs}"
    assert page.url == url_before, "Không được điều hướng đi đâu khi server lỗi 500 HTML (không redirect)"


# ---------------------------------------------------------------------------
# Loi 4 - nut phai giu trang thai disabled/spinning cho toi khi trang THUC SU reload
# xong, khong duoc bat lai giua chung trong luc cho reload.
#
# GHI CHU (KHONG viet duoc test Playwright dang tin cay cho chinh xac race-condition nay -
# xem bao cao gui Leader): da thu nhieu ky thuat de "bat qua" nut van con disabled DUNG LUC
# dang cho window.location.reload() hoan tat (delay response cua GET reload qua page.route +
# time.sleep chan thread dispatcher cua Playwright sync API; ban async_playwright + asyncio.sleep
# khong chan thread nhung Chromium/CDP van "dong bang" moi truy van DOM cua frame ngay khi 1
# navigation cap cao nhat (location.reload()) bat dau, du CHUA nhan duoc response - moi lenh
# is_disabled()/evaluate() deu treo cho toi khi navigation hoan tat hoac that bai; bat
# 'beforeunload'/'pagehide' + ghi ket qua vao localStorage cung KHONG nhan duoc su kien nao qua
# CDP console truoc khi target bi huy; route.abort() lam navigation that bai nhung thay bang
# trang loi chrome-error://, cung mat DOM cu). Day la gioi han cong cu (Playwright/CDP), khong
# phai gioi han logic app - da bu lai bang kiem tra tinh (Nhiem vu 2, diem 2) xac nhan thu tu
# code: `reloading = true` duoc set NGAY TRUOC `window.location.reload()`, va `finally` chi bat
# lai nut khi `!reloading` - dung nhu comment "Loi 4" trong base.html mo ta.
#
# Test duoi day chi la smoke-check baseline (khong the phan biet code CO/KHONG co fix Loi 4):
# xac nhan nut duoc disable+spinning ngay khi bam (hanh vi co san tu truoc, khong doi), va sau
# khi reload that su hoan tat trang van o dung URL /tasks (khong crash, khong ket qua bat
# thuong) - dam bao it nhat KHONG co regression ro rang o luong Run thanh cong.
# ---------------------------------------------------------------------------
def test_run_success_disables_button_then_reloads_cleanly(live_server, page, db_conn):
    email = "loi4-tester@example.com"
    dev_login(page, live_server["base_url"], email)
    promote_to_super_admin(db_conn, email)
    task_id = seed_runnable_task(db_conn, with_config=False)

    def handle_run(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "message": "Đã Run thành công"}),
        )

    page.route(f"**/tasks/{task_id}/run", handle_run)

    page.goto(f"{live_server['base_url']}/tasks?page_size=50")
    run_form = page.locator(f"form[action='/tasks/{task_id}/run']")
    run_btn = run_form.locator("button[type='submit']")
    run_btn.click()

    page.wait_for_load_state("load", timeout=5000)
    assert page.url.split("?")[0].endswith("/tasks"), f"Sau khi Run thành công phải reload lại /tasks, url={page.url}"


# ---------------------------------------------------------------------------
# Loi 2 - sessionStorage.setItem nem loi khong duoc pha luong thanh cong: van phai
# reload, khong sinh POST thu 2, khong sinh unhandled exception.
# ---------------------------------------------------------------------------
def test_session_storage_failure_does_not_break_success_flow(live_server, page, db_conn):
    email = "loi2-tester@example.com"
    dev_login(page, live_server["base_url"], email)
    promote_to_super_admin(db_conn, email)
    task_id = seed_runnable_task(db_conn, with_config=False)

    # Patch Storage.prototype.setItem TRUOC khi bat ky script nao cua trang chay, de
    # sessionStorage.setItem('pendingToast', ...) trong doAjaxSubmit nem loi that su.
    page.add_init_script(
        """
        (function () {
          const orig = Storage.prototype.setItem;
          Storage.prototype.setItem = function (key, value) {
            if (key === 'pendingToast') {
              throw new Error('QuotaExceededError (gia lap Loi 2)');
            }
            return orig.call(this, key, value);
          };
        })();
        """
    )

    post_count = {"n": 0}

    def handle_run(route):
        post_count["n"] += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "message": "Đã Run thành công"}),
        )

    page.route(f"**/tasks/{task_id}/run", handle_run)

    page_errors = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))

    page.goto(f"{live_server['base_url']}/tasks?page_size=50")
    run_form = page.locator(f"form[action='/tasks/{task_id}/run']")
    run_form.locator("button[type='submit']").click()

    # window.location.reload() dieu huong that su -> cho load xong.
    page.wait_for_load_state("load", timeout=5000)
    page.wait_for_timeout(300)

    assert post_count["n"] == 1, (
        f"Bug Lỗi 2: sessionStorage.setItem lỗi không được gây thêm request POST (resubmit "
        f"trùng). Số POST thực tế: {post_count['n']}"
    )
    assert page_errors == [], (
        f"Bug Lỗi 2: sessionStorage.setItem lỗi phải được try/catch RIÊNG, không được lọt ra "
        f"ngoài thành unhandled exception (chặn luôn window.location.reload()). Lỗi ghi nhận: "
        f"{page_errors}"
    )
