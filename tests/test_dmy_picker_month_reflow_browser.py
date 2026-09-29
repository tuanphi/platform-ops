"""Verify ĐỘC LẬP vòng 3 (fix sâu hơn của bug tràn viewport) của widget lịch popup
`window.initDmyPicker` (app/templates/base.html) - KHÔNG tin báo cáo của developer.

Bug gốc vòng 1/2 (đã có test riêng ở test_dmy_picker_popup_positioning_browser.py) là popup
tràn viewport ngay LÚC MỞ. Vòng 3 sửa 1 nguyên nhân SÂU HƠN: `positionPopup()` không được gọi
lại khi lưới ngày đổi từ 5 hàng sang 6 hàng (hoặc ngược lại) SAU KHI popup đã mở (do người dùng
bấm "Tháng trước/Tháng sau/Hôm nay"), khiến popup phình cao thêm và tràn ra ngoài viewport dù
lúc mở ban đầu vẫn nằm gọn.

File này CHỦ ĐỘNG lái lưới lịch qua đúng ranh giới 5 hàng <-> 6 hàng (tính bằng công thức Python
độc lập với JS, dựa trên `calendar.monthrange` + `date.weekday()` - tương đương chính xác công
thức `(firstOfMonth.getDay()+6)%7` dùng trong JS vì Python `weekday()` cũng lấy Thứ 2 = 0) rồi đo
lại bounding box SAU MỖI LẦN chuyển tháng, không chỉ đo 1 lần lúc mở như các test trước.

Môi trường / cách chạy: giống hệt 2 file browser test khác trong thư mục này (live uvicorn
subprocess + SQLite riêng + Playwright Chromium thật). Bỏ qua toàn bộ module nếu không có
Chromium.
"""

from __future__ import annotations

import calendar
import contextlib
import socket
import subprocess
import sys
import time
from datetime import date, datetime

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


def rows_for_month(year: int, month: int) -> int:
    """So hang cua luoi ngay (Thu 2 dau tuan) cho 1 thang - CONG THUC DOC LAP voi JS, dung de
    doi chieu (khong tin JS tu bao cao dung). Python date.weekday(): Thu2=0..CN=6, TRUNG voi
    cong thu JS (firstOfMonth.getDay()+6)%7 (JS getDay(): CN=0..T7=6, quy doi ve T2=0..CN=6)."""
    start_offset = date(year, month, 1).weekday()
    days_in_month = calendar.monthrange(year, month)[1]
    return -(-(start_offset + days_in_month) // 7)  # ceil div


def find_month_with_rows(start_year: int, start_month: int, target_rows: int, max_steps: int = 30):
    """Tim so buoc 'Thang sau' (>=0) can bam tu (start_year,start_month) de den 1 thang co dung
    target_rows hang. Tra ve None neu khong tim thay trong max_steps buoc (khong nen xay ra -
    thuc nghiem cho thay thang 6 hang xuat hien it nhat 1 lan trong bat ky 12 thang lien tiep)."""
    y, m = start_year, start_month
    for step in range(max_steps + 1):
        if rows_for_month(y, m) == target_rows:
            return step
        m += 1
        if m > 12:
            m = 1
            y += 1
    return None


def test_helper_rows_for_month_sane():
    """Regression-guard cho chinh ham helper (khong phai widget) - Thang 2/2026 (khong nhuan,
    28 ngay, 1/2/2026 la Chu Nhat = weekday 6) phai co dung 5 hang; Thang 2/2028 (nhuan, 29
    ngay, 1/2/2028 la Thu Ba = weekday 1) phai co dung 5 hang; Thang 8/2026 (1/8/2026 la Thu 7 =
    weekday 5, 31 ngay) phai co dung 6 hang - doi chieu tay bang lich thuc te."""
    assert rows_for_month(2026, 2) == 5
    assert rows_for_month(2028, 2) == 5
    assert rows_for_month(2026, 8) == 6


# ---------------------------------------------------------------------------
# Live server fixture (giong het 2 file browser test kia trong thu muc nay)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    work_dir = tmp_path_factory.mktemp("dmy_picker_reflow_live_server")
    db_path = work_dir / "widget_reflow_test.db"
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
        "SECRET_KEY": "test-secret-key-for-dmy-picker-reflow-browser-test",
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


def seed_confirmed_task(db_conn, email, commitid="rfw0001"):
    from app.models import DeployTask, TaskStatus

    task = DeployTask(
        project="p",
        application="a",
        commitid=commitid,
        status=TaskStatus.APPROVED,
        email=email,
        confirmed_run_at=datetime.utcnow(),
    )
    db_conn.add(task)
    db_conn.commit()
    db_conn.refresh(task)
    return task


def new_sized_page(browser, width, height):
    context = browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    return context, page


def open_task_form_popup(page, live_server, email):
    dev_login(page, live_server["base_url"], email)
    page.goto(f"{live_server['base_url']}/tasks/create")
    page.wait_for_selector("#confirmed_run_at")
    page.click(".dmy-picker-toggle")
    page.wait_for_selector(".dmy-picker-popup.open")
    page.wait_for_selector(".dmy-picker-popup.dmy-show")
    page.wait_for_timeout(250)


def open_modal_popup(page, live_server, db_conn, email, commitid):
    dev_login(page, live_server["base_url"], email)
    promote_to_super_admin(db_conn, email)
    task = seed_confirmed_task(db_conn, email, commitid=commitid)
    page.goto(f"{live_server['base_url']}/tasks")
    page.click(f'form[action="/tasks/{task.id}/auto-deploy"] .auto-toggle')
    page.wait_for_selector("#auto-deploy-modal.open")
    page.click(".dmy-picker-toggle")
    page.wait_for_selector(".dmy-picker-popup.open")
    page.wait_for_selector(".dmy-picker-popup.dmy-show")
    page.wait_for_timeout(250)
    return task


def assert_fully_inside_viewport(page, label: str):
    viewport = page.viewport_size
    for sel in [".dmy-picker-popup", ".dmy-today", ".dmy-cancel", ".dmy-ok"]:
        box = page.locator(sel).bounding_box()
        assert box is not None, f"{label}: khong lay duoc bounding box cua {sel}"
        assert box["x"] >= -0.5, f"{label}: {sel} tran sang trai viewport: {box}"
        assert box["y"] >= -0.5, f"{label}: {sel} tran len tren viewport: {box}"
        assert box["x"] + box["width"] <= viewport["width"] + 0.5, (
            f"{label}: {sel} tran sang phai viewport ({viewport}): {box}"
        )
        assert box["y"] + box["height"] <= viewport["height"] + 0.5, (
            f"{label}: {sel} tran xuong duoi viewport ({viewport}): {box}"
        )


def current_grid_rows(page) -> int:
    """Dem so hang thuc te CUA DOM (tong so .day, ke ca .muted, chia 7) - doc lap voi label
    text, dung de doi chieu voi rows_for_month() tinh o Python."""
    total = page.locator(".dmy-picker-days .day").count()
    assert total % 7 == 0, f"tong so o ngay khong chia het cho 7 ({total}) - logic luoi bi sai"
    return total // 7


def get_label_year_month(page):
    text = page.inner_text(".dmy-picker-label").strip()  # "Thang M/YYYY"
    month_str, year_str = text.replace("Tháng", "").strip().split("/")
    return int(year_str), int(month_str)


VIEWPORTS = [
    (1280, 900),
    (1366, 768),
    (1366, 900),
    (1920, 1080),
]


# ---------------------------------------------------------------------------
# Kich ban chinh: mo popup roi chuyen thang NHIEU LAN (Thang sau) cho toi khi gap
# thang 6 hang, do lai bounding box SAU MOI lan bam - dung cho ca 2 noi dung widget.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("width,height", VIEWPORTS, ids=[f"{w}x{h}" for w, h in VIEWPORTS])
def test_month_change_after_open_task_form_no_overflow(live_server, browser, width, height):
    context, page = new_sized_page(browser, width, height)
    try:
        open_task_form_popup(page, live_server, f"mr-tf-{width}x{height}@example.com")
        label = f"task_form.html @ {width}x{height}"
        assert_fully_inside_viewport(page, f"{label} (luc vua mo)")

        year, month = get_label_year_month(page)
        rows0 = current_grid_rows(page)
        assert rows0 == rows_for_month(year, month), (
            f"{label}: so hang DOM ({rows0}) khong khop cong thuc doc lap "
            f"({rows_for_month(year, month)}) cho {month}/{year}"
        )

        target_rows = 6 if rows0 != 6 else 5
        steps = find_month_with_rows(year, month, target_rows)
        assert steps is not None, f"{label}: khong tim duoc thang {target_rows} hang trong 30 buoc tu {month}/{year}"

        seen_rows = {rows0}
        for i in range(1, steps + 1):
            page.click(".dmy-next")
            page.wait_for_timeout(60)
            rows_i = current_grid_rows(page)
            seen_rows.add(rows_i)
            assert_fully_inside_viewport(page, f"{label} (sau {i} lan bam Thang sau, {rows_i} hang)")

        assert target_rows in seen_rows, f"{label}: khong dat toi thang {target_rows} hang nhu du kien tinh truoc"
        # Neu bat dau tu thang 5 hang thi phai da thay CA 5 va 6 hang trong qua trinh di chuyen.
        if rows0 == 5:
            assert seen_rows == {5, 6}, f"{label}: chua thuc su di qua ca 2 loai luoi 5/6 hang: {seen_rows}"

        # Click that vao 1 ngay + nut Chon ngay tai thang 6 hang (khong bi che/chan).
        page.click(".dmy-picker-days .day:not(.muted):text-is('20')")
        page.click(".dmy-ok")
        assert page.input_value("#confirmed_run_at").startswith("20/"), (
            f"{label}: khong click duoc nut Chon sau khi doi thang lam luoi phinh cao hon"
        )
    finally:
        context.close()


@pytest.mark.parametrize("width,height", VIEWPORTS, ids=[f"{w}x{h}" for w, h in VIEWPORTS])
def test_month_change_after_open_modal_no_overflow(live_server, browser, db_conn, width, height):
    context, page = new_sized_page(browser, width, height)
    try:
        open_modal_popup(
            page, live_server, db_conn, f"mr-md-{width}x{height}@example.com", f"mrmd{width}{height}"
        )
        label = f"task_list.html modal @ {width}x{height}"
        assert_fully_inside_viewport(page, f"{label} (luc vua mo)")

        year, month = get_label_year_month(page)
        rows0 = current_grid_rows(page)
        target_rows = 6 if rows0 != 6 else 5
        steps = find_month_with_rows(year, month, target_rows)
        assert steps is not None, f"{label}: khong tim duoc thang {target_rows} hang trong 30 buoc tu {month}/{year}"

        for i in range(1, steps + 1):
            page.click(".dmy-next")
            page.wait_for_timeout(60)
            assert_fully_inside_viewport(page, f"{label} (sau {i} lan bam Thang sau)")

        # Click that vao ngay + nut Chon, VA xac nhan modal khong bi dong ngoai y muon.
        page.click(".dmy-picker-days .day:not(.muted):text-is('20')")
        page.click(".dmy-ok")
        assert page.input_value("#auto-deploy-time").startswith("20/"), (
            f"{label}: khong click duoc nut Chon sau khi doi thang trong modal"
        )
        assert "open" in (page.get_attribute("#auto-deploy-modal", "class") or ""), (
            f"{label}: modal bi dong ngoai y muon sau khi Chon trong popup da doi thang"
        )
    finally:
        context.close()


def test_month_navigate_forward_then_back_then_today_keeps_inside_viewport(live_server, browser):
    """Bam Thang sau qua nhieu thang (bao gom it nhat 1 lan doi so hang luoi ngay), roi Thang
    truoc quay ve, roi Hom nay - moi buoc deu phai nam gon trong viewport. Dung vien thap
    (1366x768) de sat voi kich ban thuc te de xay ra bug hon la vien rong rai."""
    context, page = new_sized_page(browser, 1366, 768)
    try:
        open_task_form_popup(page, live_server, "mr-navback@example.com")
        year, month = get_label_year_month(page)
        # target_rows KHONG con co dinh = 6: neu THANG HIEN TAI (ngay thuc te luc chay test)
        # tinh co da san co 6 hang (vd thang 8/2026), tim "thang 6 hang" se ra steps=0 ngay tu
        # dau, lam sai lech y nghia test (khong con kiem tra duoc qua trinh DOI so hang luc
        # dieu huong nua) - da phat hien qua thuc nghiem (bug that, khong phai gia dinh). Chon
        # dong: uu tien 6 hang (kich ban de gay tran nhat) TRU KHI thang hien tai da la 6,
        # luc do doi sang tim 5 hang - dam bao LUON co it nhat 1 buoc di chuyen y nghia bat ke
        # thang nao dang la "hien tai" khi test chay.
        current_rows = rows_for_month(year, month)
        target_rows = 6 if current_rows != 6 else 5
        steps = find_month_with_rows(year, month, target_rows)
        assert steps is not None and steps > 0, "can it nhat 1 buoc de test co y nghia doi chieu tien/lui"

        for i in range(steps):
            page.click(".dmy-next")
            page.wait_for_timeout(60)
            assert_fully_inside_viewport(page, f"tien {i + 1}/{steps}")

        assert current_grid_rows(page) == target_rows, "chua thuc su den duoc thang co so hang nhu tinh truoc"

        for i in range(steps):
            page.click(".dmy-prev")
            page.wait_for_timeout(60)
            assert_fully_inside_viewport(page, f"lui {i + 1}/{steps}")

        y2, m2 = get_label_year_month(page)
        assert (y2, m2) == (year, month), f"Thang truoc x{steps} khong quay dung ve thang ban dau: {(y2, m2)} != {(year, month)}"

        page.click(".dmy-today")
        page.wait_for_timeout(60)
        assert_fully_inside_viewport(page, "sau khi bam Hom nay")
        today = date.today()
        y3, m3 = get_label_year_month(page)
        assert (y3, m3) == (today.year, today.month), f"Hom nay khong ve dung thang hien tai: {(y3, m3)}"
    finally:
        context.close()


def test_month_change_combined_with_low_viewport_internal_scroll(live_server, browser):
    """Ket hop 2 dieu kien xau nhat cung luc: viewport rat thap (ep popup phai tu cuon noi bo
    NGAY LUC MO) + doi sang thang 6 hang SAU do (lam popup can phinh cao THEM nua) - kich ban
    de gay tran nhat neu positionPopup() khong duoc goi lai dung cho trong renderCalendar()."""
    context, page = new_sized_page(browser, 1366, 480)
    try:
        open_task_form_popup(page, live_server, "mr-lowvp@example.com")
        assert_fully_inside_viewport(page, "1366x480 (luc vua mo, truoc khi doi thang)")

        year, month = get_label_year_month(page)
        steps = find_month_with_rows(year, month, 6)
        assert steps is not None
        for i in range(steps):
            page.click(".dmy-next")
            page.wait_for_timeout(60)
            assert_fully_inside_viewport(page, f"1366x480 (sau {i + 1} lan Thang sau, dang tien toi thang 6 hang)")
        assert current_grid_rows(page) == 6

        # Hang nut sticky van phai hien/click duoc that su o trang thai xau nhat nay.
        page.click(".dmy-picker-days .day:not(.muted):text-is('20')")
        page.click(".dmy-ok")
        assert page.input_value("#confirmed_run_at").startswith("20/"), (
            "khong click duoc nut Chon (sticky) o to hop viewport thap + thang 6 hang"
        )
    finally:
        context.close()


def test_no_new_console_errors_during_month_navigation(live_server, browser):
    context, page = new_sized_page(browser, 1366, 768)
    errors = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    try:
        open_task_form_popup(page, live_server, "mr-console@example.com")
        for _ in range(14):
            page.click(".dmy-next")
            page.wait_for_timeout(30)
        for _ in range(14):
            page.click(".dmy-prev")
            page.wait_for_timeout(30)
        page.click(".dmy-today")
        page.wait_for_timeout(50)
        assert not errors, f"Co loi JS moi phat sinh trong luc dieu huong thang: {errors}"
    finally:
        context.close()
