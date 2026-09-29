"""Kiểm thử hành vi JS/browser của tính năng auto-refresh trạng thái task
(app/templates/task_list.html, khối `<script>` cuối file - polling nhẹ tới
`GET /tasks/api/statuses` + refetch HTML khi có thay đổi), commit 0d412f8.

Đây LÀ browser test thật (Playwright/Chromium) - cần 1 server FastAPI thật chạy trên
subprocess (uvicorn), theo đúng pattern/hạ tầng đã có sẵn của
`tests/test_dmy_picker_widget_browser.py` (live_server fixture, dev-login qua
AUTH_DEV_MODE, SQLite file riêng KHÔNG đụng form_deploy.db thật hay in-memory DB của
tests/conftest.py).

Kỹ thuật rút ngắn thời gian chờ: POLL_INTERVAL_MS trong task_list.html = 10000 (10s) -
để test không phải chờ thật 10s+/lần, dùng `page.add_init_script` ghi đè
`window.setInterval` để ép MỌI lời gọi setInterval (bất kể delay được truyền) chạy với
delay ngắn (300ms). Việc ghi đè này chạy TRƯỚC khi bất kỳ script nào của trang thực thi
(add_init_script chạy ở document-start), nên không ảnh hưởng tới logic polling thật -
chỉ ảnh hưởng tốc độ, không ảnh hưởng hành vi (không đổi code polling, không mock
fetch()).

Bỏ qua toàn bộ module nếu không có Chromium cài sẵn cho Playwright."""

from __future__ import annotations

import contextlib
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


# ---------------------------------------------------------------------------
# Live server fixture: uvicorn subprocess trỏ vào 1 SQLite file riêng của MODULE NÀY -
# cô lập hoàn toàn khỏi form_deploy.db thật, khỏi in-memory DB của tests/conftest.py, và
# khỏi file DB của test_dmy_picker_widget_browser.py (mỗi module dùng tmp_path riêng).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    work_dir = tmp_path_factory.mktemp("task_status_polling_live_server")
    db_path = work_dir / "polling_test.db"
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
        "SECRET_KEY": "test-secret-key-for-task-status-polling-browser-test",
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
    context = browser.new_context(viewport={"width": 1280, "height": 1400})
    pg = context.new_page()
    # Ep MOI setInterval() chay voi delay ngan (300ms) bat ke delay goc (POLL_INTERVAL_MS
    # =10000 trong task_list.html) - chi rut ngan thoi gian cho, KHONG doi logic polling.
    # Phai add truoc goto() de chay o document-start, truoc script cua trang.
    pg.add_init_script(
        """
        (() => {
          const _origSetInterval = window.setInterval;
          window.setInterval = function(fn, _delay, ...args) {
            return _origSetInterval(fn, 300, ...args);
          };
        })();
        """
    )
    yield pg
    context.close()


def dev_login(page, base_url: str, email: str) -> None:
    page.goto(f"{base_url}/auth/login")
    page.fill('input[name="email"]', email)
    page.click('form[action="/auth/dev-login"] button[type="submit"]')
    page.wait_for_url(f"{base_url}/home")


def promote_to_super_admin(db_conn, email: str) -> None:
    from app.models import Role, User

    user = db_conn.query(User).filter(User.email == email).one()
    user.role = Role.SUPER_ADMIN
    db_conn.commit()


def seed_task(db_conn, email, status, commitid="pol0001", **kwargs):
    from app.models import DeployTask, TaskStatus

    defaults = dict(
        project="p",
        application="a",
        commitid=commitid,
        status=status if isinstance(status, TaskStatus) else TaskStatus(status),
        email=email,
    )
    defaults.update(kwargs)
    task = DeployTask(**defaults)
    db_conn.add(task)
    db_conn.commit()
    db_conn.refresh(task)
    return task


def set_task_status(db_conn, task_id, status):
    """Doi status truc tiep trong DB, mo phong scheduler chay ngam (job_check_running_done)
    doi Running -> Done, khong qua bat ky request HTTP nao - dung y nghia cua tinh nang:
    UI phai tu phat hien thay doi tu nguon ben ngoai request cua chinh no."""
    from app.models import DeployTask, TaskStatus

    db_conn.query(DeployTask).filter(DeployTask.id == task_id).update({"status": TaskStatus(status)})
    db_conn.commit()


def collect_requests(page, path_substring: str):
    hits = []

    def _on_request(req):
        if path_substring in req.url:
            hits.append(req.url)

    page.on("request", _on_request)
    return hits


# ---------------------------------------------------------------------------
# 9. Khong co thay doi -> chi co request toi /tasks/api/statuses, KHONG fetch lai HTML.
# ---------------------------------------------------------------------------


def test_no_change_only_status_endpoint_polled_no_html_refetch(live_server, page, db_conn):
    dev_login(page, live_server["base_url"], "poll9@example.com")
    promote_to_super_admin(db_conn, "poll9@example.com")
    task = seed_task(db_conn, "poll9@example.com", "Task", commitid="poll0009")

    page.goto(f"{live_server['base_url']}/tasks")
    page.wait_for_selector(f'tr[data-id="{task.id}"]')

    api_hits = collect_requests(page, "/tasks/api/statuses")
    html_refetch_hits = []

    def _on_request(req):
        # Fetch lai HTML trang hien tai dung fetch(location.pathname + location.search)
        # voi header Accept: text/html - phan biet voi navigation request ban dau (da xay
        # ra truoc khi listener nay duoc gan) bang resource_type == "fetch".
        if req.resource_type == "fetch" and req.url.rstrip("/").endswith("/tasks"):
            html_refetch_hits.append(req.url)

    page.on("request", _on_request)

    # Doi vai chu ky poll (da rut ngan con 300ms/lan qua add_init_script).
    page.wait_for_timeout(1500)

    assert len(api_hits) >= 2, f"khong thay du request toi /tasks/api/statuses: {api_hits}"
    assert html_refetch_hits == [], f"KHONG duoc fetch lai HTML khi khong co thay doi status: {html_refetch_hits}"

    # Badge van giu nguyen status cu, khong bi doi/mat.
    assert "Task" in page.locator(f'tr[data-id="{task.id}"] .badge').inner_text()


# ---------------------------------------------------------------------------
# 10. Status doi o server -> UI tu cap nhat badge + nut, KHONG full page reload.
# ---------------------------------------------------------------------------


def test_status_change_updates_badge_without_full_reload(live_server, page, db_conn):
    dev_login(page, live_server["base_url"], "poll10@example.com")
    promote_to_super_admin(db_conn, "poll10@example.com")
    task = seed_task(db_conn, "poll10@example.com", "Approved", commitid="poll0010")

    page.goto(f"{live_server['base_url']}/tasks")
    page.wait_for_selector(f'tr[data-id="{task.id}"]')
    badge = page.locator(f'tr[data-id="{task.id}"] .badge')
    assert badge.inner_text().strip() == "Approved"

    navigations = []
    page.on("framenavigated", lambda frame: navigations.append(frame.url) if frame == page.main_frame else None)

    # Mo phong scheduler doi status ngam - khong qua request HTTP nao ca.
    set_task_status(db_conn, task.id, "Running")

    page.wait_for_function(
        "sel => document.querySelector(sel) && document.querySelector(sel).textContent.trim() === 'Running'",
        arg=f'tr[data-id="{task.id}"] .badge',
        timeout=5000,
    )

    assert page.locator(f'tr[data-id="{task.id}"]').get_attribute("data-status") == "Running"
    # Khong co dieu huong/reload nao xay ra (URL frame khong doi lai chinh no qua navigation
    # - list rong nghia la khong co full page reload sau lan goto ban dau).
    assert navigations == [], f"Phat hien full page reload thay vi cap nhat DOM tai cho: {navigations}"


# ---------------------------------------------------------------------------
# 11. Tab an (visibilityState = 'hidden') -> khong goi fetch nao ca.
# ---------------------------------------------------------------------------


def test_hidden_tab_does_not_poll(live_server, page, db_conn):
    dev_login(page, live_server["base_url"], "poll11@example.com")
    promote_to_super_admin(db_conn, "poll11@example.com")
    task = seed_task(db_conn, "poll11@example.com", "Task", commitid="poll0011")

    # Ghi de document.visibilityState = 'hidden' TRUOC khi script cua trang chay, de
    # poll() (kiem tra document.visibilityState !== 'visible') luon return som.
    page.add_init_script(
        """
        Object.defineProperty(document, 'visibilityState', { get: () => 'hidden', configurable: true });
        """
    )

    api_hits = []
    page.on("request", lambda req: api_hits.append(req.url) if "/tasks/api/statuses" in req.url else None)

    page.goto(f"{live_server['base_url']}/tasks")
    page.wait_for_selector(f'tr[data-id="{task.id}"]')
    page.wait_for_timeout(1500)

    assert api_hits == [], f"Khong duoc goi fetch toi /tasks/api/statuses khi tab an: {api_hits}"


# ---------------------------------------------------------------------------
# 12. Modal Auto-deploy dang mo luc co thay doi -> khong vo/mat form, poll tri hoan.
# ---------------------------------------------------------------------------


def test_modal_open_defers_tbody_swap_until_closed(live_server, page, db_conn):
    dev_login(page, live_server["base_url"], "poll12@example.com")
    promote_to_super_admin(db_conn, "poll12@example.com")
    task = seed_task(db_conn, "poll12@example.com", "Task", commitid="poll0012")

    page.goto(f"{live_server['base_url']}/tasks")
    page.wait_for_selector(f'tr[data-id="{task.id}"]')

    page.click(f'form[action="/tasks/{task.id}/auto-deploy"] .auto-toggle')
    page.wait_for_selector("#auto-deploy-modal.open")

    html_refetch_hits = []
    page.on(
        "request",
        lambda req: html_refetch_hits.append(req.url)
        if req.resource_type == "fetch" and req.url.rstrip("/").endswith("/tasks")
        else None,
    )

    # Doi status server trong khi modal dang mo.
    set_task_status(db_conn, task.id, "Approved")

    # Doi qua vai chu ky poll (300ms/lan) - badge PHAI CHUA doi vi modal dang mo (poll bi
    # tri hoan o buoc kiem tra isAutoDeployModalOpen()), modal PHAI VAN con mo va form
    # picker gio khong bi mat/detach.
    page.wait_for_timeout(1200)

    assert (
        page.locator(f'tr[data-id="{task.id}"]').get_attribute("data-status") == "Task"
    ), "tbody bi thay the du modal Auto-deploy dang mo - co the lam vo/mat form dang thao tac"
    assert "open" in (page.get_attribute("#auto-deploy-modal", "class") or "")
    assert page.locator("#auto-deploy-time").count() == 1, "form/input trong modal bi mat sau khi tbody bi thay the"
    assert html_refetch_hits == [], f"Khong duoc fetch lai HTML trong khi modal dang mo: {html_refetch_hits}"

    # Dong modal -> luot poll ke tiep phai ap dung thay doi da bi tri hoan.
    page.click("#auto-deploy-cancel")
    page.wait_for_function(
        "() => !document.getElementById('auto-deploy-modal').classList.contains('open')"
    )
    page.wait_for_function(
        "sel => document.querySelector(sel) && document.querySelector(sel).getAttribute('data-status') === 'Approved'",
        arg=f'tr[data-id="{task.id}"]',
        timeout=5000,
    )


# ---------------------------------------------------------------------------
# 13. Trang toan task Done/Rejected/Cancelled -> khong set interval polling.
# ---------------------------------------------------------------------------


def test_all_terminal_tasks_no_polling_interval_set(live_server, page, db_conn):
    # KHONG promote len super admin: co chu dinh giu la plain user KHONG duoc grant gi -
    # view-scope filter (_view_scope_filter) khi do chi hien task cua CHINH email nay,
    # tranh bi "ro ri" cac task terminal-status khac cua cac test truoc trong CUNG 1 file
    # DB (live_server scope=module, dung chung 1 SQLite file cho ca module) - neu dung
    # super admin (full-access) se thay TOAN BO task he thong (gom ca task con dang chay
    # "Running"/"Approved" tao boi cac test khac chay truoc trong module), lam sai lech
    # kich ban "toan bo task tren trang deu terminal".
    dev_login(page, live_server["base_url"], "poll13@example.com")
    seed_task(db_conn, "poll13@example.com", "Done", commitid="poll0013", done_at=datetime.utcnow())
    seed_task(db_conn, "poll13@example.com", "Rejected", commitid="poll0014")
    seed_task(db_conn, "poll13@example.com", "Cancelled", commitid="poll0015")

    api_hits = []
    page.on("request", lambda req: api_hits.append(req.url) if "/tasks/api/statuses" in req.url else None)

    page.goto(f"{live_server['base_url']}/tasks")
    page.wait_for_selector('tr[data-id]')
    # Cho du lau de neu (sai) co interval duoc set voi delay da rut ngan (300ms) thi chac
    # chan se bat duoc it nhat 1 request trong khoang thoi gian nay.
    page.wait_for_timeout(1500)

    assert api_hits == [], f"KHONG duoc polling khi tat ca task deu o trang thai terminal: {api_hits}"


# ---------------------------------------------------------------------------
# 14. Regression nhe: khong co console error khi polling chay (load + 1 vong doi status).
# ---------------------------------------------------------------------------


def test_no_console_errors_during_polling_cycle_with_status_change(live_server, page, db_conn):
    errors = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    dev_login(page, live_server["base_url"], "poll14@example.com")
    promote_to_super_admin(db_conn, "poll14@example.com")
    task = seed_task(db_conn, "poll14@example.com", "Task", commitid="poll0016")

    page.goto(f"{live_server['base_url']}/tasks")
    page.wait_for_selector(f'tr[data-id="{task.id}"]')

    set_task_status(db_conn, task.id, "Done")
    page.wait_for_function(
        "sel => document.querySelector(sel) && document.querySelector(sel).getAttribute('data-status') === 'Done'",
        arg=f'tr[data-id="{task.id}"]',
        timeout=5000,
    )
    page.wait_for_timeout(300)

    assert not errors, f"Console errors trong luc polling cap nhat status: {errors}"
