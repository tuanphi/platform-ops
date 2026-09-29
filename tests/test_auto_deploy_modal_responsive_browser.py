"""[TEST] Happy-path only - kiem chung fix CSS `.modal-box` (app/templates/base.html, box-sizing:
border-box + max-width 360->408px) cho modal `#auto-deploy-modal` (app/templates/task_list.html).

Chi 3 test case theo yeu cau: mobile 375px, mobile/tablet 768px, desktop 1280px - do
getBoundingClientRect() cua .modal-box bang Playwright Chromium THAT (khong doan CSS suong),
xac nhan modal khong tran ra ngoai viewport va tren desktop van ~408px nhu truoc.

Setup (live uvicorn subprocess + SQLite rieng + dev-login) sao chep dung pattern da co san o
tests/test_dmy_picker_popup_positioning_browser.py de dam bao khop convention cua repo. Bo qua
toan bo module neu khong co Chromium (giong het cac browser test khac trong repo).
"""

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
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent


def _chromium_available() -> bool:
    if sync_playwright is None:
        return False
    try:
        with sync_playwright() as p:
            p.chromium.launch(headless=True).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _chromium_available(),
    reason="Playwright Chromium chua cai san (python -m playwright install chromium).",
)


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    work_dir = tmp_path_factory.mktemp("auto_deploy_modal_responsive")
    db_path = work_dir / "test.db"
    db_url = f"sqlite:///{db_path}"
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    base_url = f"http://127.0.0.1:{port}"

    sys.path.insert(0, str(REPO_ROOT))
    import app.models as models_module

    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    models_module.Base.metadata.create_all(engine)
    engine.dispose()

    env = {
        "PATH": __import__("os").environ.get("PATH", ""),
        "DATABASE_URL": db_url,
        "SECRET_KEY": "test-secret-key-auto-deploy-modal-responsive",
        "AUTH_DEV_MODE": "true",
        "APP_URL": base_url,
        "ENABLE_SCHEDULER": "false",
        "ENABLE_MAIL": "false",
        "GIT_REMOTE_CREDENTIALS": "",
    }

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
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
        pytest.fail(f"Live server khong khoi dong duoc (err={last_err}).\n{out}")

    yield {"base_url": base_url, "db_url": db_url}

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


def _open_auto_deploy_modal(browser, live_server, db_conn, width, height, email, commitid):
    from app.models import DeployTask, Role, TaskStatus, User

    context = browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()

    page.goto(f"{live_server['base_url']}/auth/login")
    page.fill('input[name="email"]', email)
    page.click('form[action="/auth/dev-login"] button[type="submit"]')
    page.wait_for_url(f"{live_server['base_url']}/home")

    user = db_conn.query(User).filter(User.email == email).one()
    user.role = Role.SUPER_ADMIN
    db_conn.commit()

    task = DeployTask(
        project="p", application="a", commitid=commitid,
        status=TaskStatus.APPROVED, email=email,
        confirmed_run_at=datetime.utcnow(),
    )
    db_conn.add(task)
    db_conn.commit()
    db_conn.refresh(task)

    page.goto(f"{live_server['base_url']}/tasks")
    page.click(f'form[action="/tasks/{task.id}/auto-deploy"] .auto-toggle')
    page.wait_for_selector("#auto-deploy-modal.open")
    page.wait_for_timeout(200)  # doi animation modal-in (0.15s) chay xong truoc khi do
    return context, page


@pytest.mark.parametrize(
    "width,height,expect_max_width",
    [
        (375, 812, False),   # TC1: mobile nho
        (768, 1024, False),  # TC2: mobile/tablet
        (1280, 800, True),   # TC3: desktop - phai giu nguyen ~408px nhu truoc fix
    ],
    ids=["mobile-375", "tablet-768", "desktop-1280"],
)
def test_auto_deploy_modal_not_overflow_and_desktop_unchanged(
    browser, live_server, db_conn, width, height, expect_max_width
):
    context, page = _open_auto_deploy_modal(
        browser, live_server, db_conn, width, height,
        email=f"tester-{width}@example.com", commitid=f"resp{width}",
    )
    try:
        modal_box = page.locator("#auto-deploy-modal .modal-box").bounding_box()
        time_input_box = page.locator("#auto-deploy-time").bounding_box()
        ok_btn_box = page.locator("#auto-deploy-ok").bounding_box()
        cancel_btn_box = page.locator("#auto-deploy-cancel").bounding_box()

        assert modal_box is not None, "modal-box khong render duoc bounding box"

        # Khong tran ra ngoai 2 canh trai/phai man hinh (day chinh la bug goc).
        assert modal_box["x"] >= 0, (
            f"modal-box tran mep TRAI: x={modal_box['x']} (viewport width={width})"
        )
        assert modal_box["x"] + modal_box["width"] <= width + 0.5, (
            f"modal-box tran mep PHAI: right={modal_box['x'] + modal_box['width']} "
            f"> viewport width={width}"
        )

        # input va hang nut ben trong cung khong duoc tran.
        for name, box in (
            ("#auto-deploy-time", time_input_box),
            ("#auto-deploy-ok", ok_btn_box),
            ("#auto-deploy-cancel", cancel_btn_box),
        ):
            assert box is not None, f"{name} khong render duoc bounding box"
            assert box["x"] >= 0 and box["x"] + box["width"] <= width + 0.5, (
                f"{name} tran ra ngoai viewport (width={width}): box={box}"
            )

        if expect_max_width:
            # Desktop: border-box width phai ~408px (khong bi be lai nho hon truoc fix,
            # va khong phinh to hon do loi tinh toan).
            assert 400 <= modal_box["width"] <= 412, (
                f"desktop modal-box width={modal_box['width']} khong con ~408px nhu truoc fix"
            )
        else:
            # Mobile/tablet hep hon 408+padding overlay: modal phai TU CO LAI dung khong
            # gian con lai (overlay co padding 1rem = 16px moi ben) thay vi giu cung 408.
            assert modal_box["width"] <= width - 32 + 0.5, (
                f"modal-box (width={modal_box['width']}) khong co lai theo viewport hep "
                f"(width={width}), van tran qua padding cua modal-overlay"
            )
    finally:
        context.close()
