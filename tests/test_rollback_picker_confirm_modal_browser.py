"""[TEST] Kiểm thử happy-path bằng trình duyệt thật (Playwright/Chromium) cho thay đổi
form rollback trong app/templates/rollback_picker.html: chuyển từ
`onsubmit="return confirm(...)"` native sang cơ chế modal `data-confirm` +
`data-confirm-style="danger"` (tái dùng handler chung trong app/templates/base.html).

Kiểm tra:
  1. Bấm "Rollback về đây" -> modal xác nhận hiện lên đúng nội dung, đúng style 'danger'.
  2. Bấm Huỷ -> modal đóng lại, KHÔNG có request POST nào tới /tasks/{id}/rollback.
  3. Bấm "Rollback về đây" lần nữa rồi bấm Xác nhận -> form submit đúng target
     /tasks/{id}/rollback kèm target_task_id đúng candidate đã chọn (network interception).
  4. Không còn dùng confirm() native / onsubmit inline trong markup render ra.

Bỏ qua toàn bộ module nếu môi trường chưa cài sẵn Chromium cho Playwright (pattern giống
tests/test_staging_production_filter_sync_ui_browser.py).
"""

from __future__ import annotations

import contextlib
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta

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
    work_dir = tmp_path_factory.mktemp("rollback_confirm_modal_live_server")
    db_path = work_dir / "rollback_test.db"
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
        "SECRET_KEY": "test-secret-key-for-rollback-confirm-modal-browser-test",
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


def seed_rollback_tasks(db_conn) -> tuple[int, int]:
    """Tạo 1 task Done hiện tại (`current`) + 1 candidate Done cũ hơn (`candidate`) cùng
    project/application, để rollback_picker() render ra >= 1 dòng candidate.
    Trả về (current_task_id, candidate_task_id)."""
    from app.models import DeployTask, TaskStatus

    now = datetime.utcnow()
    candidate = DeployTask(
        project="core",
        application="api-service",
        commitid="abc1234",
        image_version="v1.0.0",
        status=TaskStatus.DONE,
        email="requester@example.com",
        maintainer_run="tester@example.com",
        run_at=now - timedelta(minutes=10),
        done_at=now - timedelta(minutes=9),
    )
    db_conn.add(candidate)
    db_conn.commit()
    db_conn.refresh(candidate)

    current = DeployTask(
        project="core",
        application="api-service",
        commitid="def5678",
        image_version="v1.1.0",
        status=TaskStatus.DONE,
        email="requester@example.com",
        maintainer_run="tester@example.com",
        run_at=now - timedelta(minutes=5),
        done_at=now - timedelta(minutes=4),
    )
    db_conn.add(current)
    db_conn.commit()
    db_conn.refresh(current)

    return current.id, candidate.id


def test_rollback_confirm_modal_full_flow(live_server, page, db_conn):
    email = "rollback-tester@example.com"
    dev_login(page, live_server["base_url"], email)
    promote_to_super_admin(db_conn, email)
    current_id, candidate_id = seed_rollback_tasks(db_conn)

    page.goto(f"{live_server['base_url']}/tasks/{current_id}/rollback")
    page.wait_for_selector("form[action='/tasks/%d/rollback']" % current_id)

    form = page.locator("form[action='/tasks/%d/rollback']" % current_id)
    assert form.count() == 1, "Không tìm thấy form rollback cho candidate đã seed"

    # --- (4) Markup không còn confirm() native / onsubmit inline ---
    onsubmit_attr = form.get_attribute("onsubmit")
    assert onsubmit_attr is None, f"Form rollback vẫn còn onsubmit inline: {onsubmit_attr!r}"
    data_confirm = form.get_attribute("data-confirm")
    expected_msg = f"Xác nhận rollback core/api-service về version v1.0.0 (task #{candidate_id})?"
    assert data_confirm == expected_msg, f"data-confirm sai nội dung: {data_confirm!r}"
    assert form.get_attribute("data-confirm-style") == "danger"

    hidden_target = form.locator("input[name='target_task_id']")
    assert hidden_target.get_attribute("value") == str(candidate_id)

    # --- (1) Bấm nút -> modal hiện đúng nội dung qua textContent (không lỗi render) ---
    submit_btn = form.locator("button[type='submit']")
    submit_btn.click()

    overlay = page.locator("#confirm-modal")
    page.wait_for_selector("#confirm-modal.open")
    assert overlay.evaluate("el => el.classList.contains('open')") is True

    msg_el = page.locator("#confirm-modal-message")
    assert msg_el.inner_text() == expected_msg, f"Nội dung modal sai: {msg_el.inner_text()!r}"

    ok_btn = page.locator("#confirm-modal-ok")
    assert ok_btn.get_attribute("class") == "danger", "Nút Xác nhận phải mang class 'danger'"

    # --- (2) Bấm Huỷ -> modal đóng, KHÔNG có request POST nào submit ---
    posted_urls = []
    page.on("request", lambda req: posted_urls.append(req.url) if req.method == "POST" else None)

    page.click("#confirm-modal-cancel")
    page.wait_for_timeout(200)
    assert overlay.evaluate("el => el.classList.contains('open')") is False, "Modal phải đóng lại sau khi bấm Huỷ"
    assert not any("/rollback" in u for u in posted_urls), (
        f"Bấm Huỷ nhưng vẫn có request POST rollback: {posted_urls}"
    )
    # Vẫn còn ở đúng trang picker (không bị điều hướng đi đâu do form lỡ submit).
    assert f"/tasks/{current_id}/rollback" in page.url

    # --- (3) Bấm lại nút rồi Xác nhận -> submit đúng target /tasks/{id}/rollback ---
    submit_btn.click()
    page.wait_for_selector("#confirm-modal.open")
    page.click("#confirm-modal-ok")

    page.wait_for_url(lambda url: "/tasks/" in url and "/rollback" not in url, timeout=10000)
    assert any(f"/tasks/{current_id}/rollback" in u for u in posted_urls), (
        f"Bấm Xác nhận nhưng không thấy request POST tới /tasks/{current_id}/rollback: {posted_urls}"
    )

    # Rollback thành công tạo task mới -> redirect về trang chi tiết task mới, có toast OK.
    assert "ok=1" in page.url or page.url.endswith(f"/tasks/{current_id}") is False
