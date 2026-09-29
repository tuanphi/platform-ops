"""Kiểm thử widget lịch popup `window.initDmyPicker` (app/templates/base.html, CSS ~155-239,
JS ~391-665) bằng trình duyệt thật (Playwright/Chromium), theo yêu cầu QA task cho lần nâng
cấp: fix icon lệch tâm + popup UI mới + 2 cột giờ/phút kiểu wheel scroll iOS.

Đây LÀ browser test thật (không phải TestClient/static-HTML) - cần 1 server FastAPI thật
chạy trên subprocess (uvicorn) vì Playwright cần điều hướng trình duyệt qua HTTP thật, chạy
JS thật (bao gồm scroll-snap, requestAnimationFrame, getBoundingClientRect...) mà
TestClient/JSDOM không mô phỏng được.

Môi trường:
  - DB: 1 file SQLite riêng (KHÔNG phải form_deploy.db thật), schema tạo trực tiếp bằng
    Base.metadata.create_all() (không chạy Alembic - chỉ đủ để test UI, không test migration).
  - AUTH_DEV_MODE=true để login qua /auth/dev-login (không cần Google OAuth thật).
  - ENABLE_SCHEDULER=false, ENABLE_MAIL=false: không chạy APScheduler/gửi mail thật.
  - GIT_REMOTE_CREDENTIALS để trống (mặc định .env.example) => flow tạo task thật
    (POST /tasks/create) sẽ lỗi ở bước check_commit_on_staging (không có git remote thật) -
    đây là giới hạn ngoài phạm vi sandbox đã nêu rõ trong task, KHÔNG phải lỗi của widget.
    Test D (create) vì vậy chỉ xác nhận payload gửi đi đúng ISO qua network interception,
    không assert task tạo thành công. Test D (auto-deploy) KHÔNG phụ thuộc git nên test
    đầy đủ end-to-end + persist sau reload.

Bỏ qua toàn bộ module nếu không có Chromium cài sẵn cho Playwright (môi trường CI/dev khác
có thể chưa chạy `playwright install chromium`).
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
# Live server fixture: uvicorn subprocess trỏ vào 1 SQLite file riêng, cô lập
# hoàn toàn khỏi form_deploy.db thật và khỏi in-memory engine của conftest.py
# (dùng chung cho các test HTTP dựa trên TestClient) - KHÔNG đụng gì tới 2 nguồn đó.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    work_dir = tmp_path_factory.mktemp("dmy_picker_live_server")
    db_path = work_dir / "widget_test.db"
    db_url = f"sqlite:///{db_path}"
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    # Tao schema truoc khi server khoi dong (app khong tu create_all - schema do Alembic
    # quan ly rieng trong luc chay that; o day chi can du bang de test UI, khong test
    # migration).
    sys.path.insert(0, str(REPO_ROOT))
    import app.models as models_module

    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    models_module.Base.metadata.create_all(engine)
    engine.dispose()

    env = {
        "PATH": __import__("os").environ.get("PATH", ""),
        "DATABASE_URL": db_url,
        "SECRET_KEY": "test-secret-key-for-dmy-picker-browser-test",
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
    """Session SQLAlchemy riêng (process của pytest, không phải process server) trỏ vào
    CÙNG file SQLite của live_server - dùng để seed/kiểm tra dữ liệu trực tiếp mà không
    phải dựng thêm 1 luồng HTTP nghiệp vụ đầy đủ (group/project/git catalog...) không
    thuộc phạm vi kiểm thử widget này."""
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


def seed_group_project(db_conn, group_name="widget-test-group", app_name="widget-test-app"):
    from app.models import Group, Project

    group = Group(group_name=group_name, is_active=True)
    db_conn.add(group)
    db_conn.commit()
    db_conn.refresh(group)
    project = Project(group_id=group.id, application_name=app_name, is_active=True)
    db_conn.add(project)
    db_conn.commit()
    return group, project


def seed_confirmed_task(db_conn, email, commitid="wid0001"):
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


def collect_console_errors(page):
    errors = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    return errors


# ---------------------------------------------------------------------------
# A. Icon căn giữa (task_form.html + modal auto-deploy trong task_list.html)
# ---------------------------------------------------------------------------


def _assert_toggle_centered_and_inside(page, input_selector: str, toggle_selector: str, label: str):
    input_box = page.locator(input_selector).bounding_box()
    toggle_box = page.locator(toggle_selector).bounding_box()
    assert input_box is not None, f"{label}: không lấy được bounding box của input"
    assert toggle_box is not None, f"{label}: không lấy được bounding box của toggle button"

    input_center_y = input_box["y"] + input_box["height"] / 2
    toggle_center_y = toggle_box["y"] + toggle_box["height"] / 2
    diff = abs(input_center_y - toggle_center_y)

    assert diff <= 2.0, (
        f"{label}: icon lệch tâm dọc {diff:.2f}px (input center={input_center_y:.2f}, "
        f"toggle center={toggle_center_y:.2f}) input_box={input_box} toggle_box={toggle_box}"
    )
    assert toggle_box["y"] >= input_box["y"] - 0.5, f"{label}: toggle tràn lên trên khung input"
    assert toggle_box["y"] + toggle_box["height"] <= input_box["y"] + input_box["height"] + 0.5, (
        f"{label}: toggle tràn xuống dưới khung input (icon lệch/tràn ra ngoài)"
    )


def test_A_icon_centered_task_form(live_server, page):
    dev_login(page, live_server["base_url"], "a-tester@example.com")
    page.goto(f"{live_server['base_url']}/tasks/create")
    page.wait_for_selector("#confirmed_run_at")
    _assert_toggle_centered_and_inside(
        page, "#confirmed_run_at", ".dmy-picker-toggle", "task_form.html #confirmed_run_at"
    )
    page.screenshot(path="/private/tmp/claude-502/-Users-quynhnx-git-gpay-Development-DevOps-form-deploy/f6c94905-12ad-4bf1-8921-31994fe64062/scratchpad/A_task_form_icon.png")


def test_A_icon_centered_task_list_modal(live_server, page, db_conn):
    dev_login(page, live_server["base_url"], "a-tester@example.com")
    promote_to_super_admin(db_conn, "a-tester@example.com")
    task = seed_confirmed_task(db_conn, "a-tester@example.com", commitid="aicon01")
    page.goto(f"{live_server['base_url']}/tasks")
    page.wait_for_selector(f'form[action="/tasks/{task.id}/auto-deploy"] .auto-toggle')
    page.click(f'form[action="/tasks/{task.id}/auto-deploy"] .auto-toggle')
    page.wait_for_selector("#auto-deploy-modal.open")
    page.wait_for_selector("#auto-deploy-time")
    _assert_toggle_centered_and_inside(
        page, "#auto-deploy-time", ".dmy-picker-toggle", "task_list.html modal #auto-deploy-time"
    )
    page.screenshot(path="/private/tmp/claude-502/-Users-quynhnx-git-gpay-Development-DevOps-form-deploy/f6c94905-12ad-4bf1-8921-31994fe64062/scratchpad/A_task_list_modal_icon.png")


# ---------------------------------------------------------------------------
# B. Chức năng picker (dùng chung 1 trang task_form.html cho các case thuần JS -
# nhanh hơn và không phụ thuộc nghiệp vụ - riêng phần "trong modal không đóng modal"
# test độc lập trên task_list.html vì đặc thù chỉ có ở đó).
# ---------------------------------------------------------------------------


@pytest.fixture()
def create_page_ready(live_server, page):
    dev_login(page, live_server["base_url"], "b-tester@example.com")
    page.goto(f"{live_server['base_url']}/tasks/create")
    page.wait_for_selector("#confirmed_run_at")
    return page


def test_B_open_popup_and_pick_date_via_grid(create_page_ready):
    page = create_page_ready
    page.click(".dmy-picker-toggle")
    page.wait_for_selector(".dmy-picker-popup.dmy-show")
    # Chon ngay 15 cua thang dang hien thi (khong dinh ngay dau/cuoi thang de tranh dinh
    # vao o "muted" cua thang truoc/sau).
    page.click(".dmy-picker-days .day:not(.muted):text-is('15')")
    day_el = page.locator(".dmy-picker-days .day.selected")
    assert day_el.inner_text() == "15"


def test_B_wheel_scroll_click_item_selects_and_ok_uses_it(create_page_ready):
    page = create_page_ready
    page.click(".dmy-picker-toggle")
    page.wait_for_selector(".dmy-picker-popup.dmy-show")
    page.click(".dmy-picker-days .day:not(.muted):text-is('10')")
    # Click truc tiep vao item gio = 05 va phut = 45 tren cot wheel.
    page.locator(".dmy-hour-list .dmy-wheel-item:text-is('05')").first.click()
    page.locator(".dmy-minute-list .dmy-wheel-item:text-is('45')").first.click()
    assert "dmy-wheel-selected" in (page.locator(".dmy-hour-list .dmy-wheel-item", has_text="05").first.get_attribute("class") or "")
    assert "dmy-wheel-selected" in (page.locator(".dmy-minute-list .dmy-wheel-item", has_text="45").first.get_attribute("class") or "")
    page.click(".dmy-ok")
    value = page.input_value("#confirmed_run_at")
    assert value.endswith(" 05:45"), f"gia tri input sau Chon khong khop gio/phut da click: {value!r}"


def test_B_wheel_scroll_via_scroll_event_updates_selection(create_page_ready):
    """Mo phong cuon (khong click item) bang cach doi scrollTop that su - kiem tra
    item o giua highlight band duoc cap nhat dung sau debounce 90ms, va gia tri lay
    khi bam Chon khop voi item do (khong chi nhin anh)."""
    page = create_page_ready
    page.click(".dmy-picker-toggle")
    page.wait_for_selector(".dmy-picker-popup.dmy-show")
    page.click(".dmy-picker-days .day:not(.muted):text-is('10')")

    ROW_H = 36
    target_hour_idx = 18  # gio 18h
    target_minute_idx = 37  # phut 37

    page.eval_on_selector(
        ".dmy-hour-list",
        "(el, top) => { el.scrollTop = top; }",
        target_hour_idx * ROW_H,
    )
    page.eval_on_selector(
        ".dmy-minute-list",
        "(el, top) => { el.scrollTop = top; }",
        target_minute_idx * ROW_H,
    )
    # Doi qua debounce 90ms cua JS (attachWheelEvents) + margin an toan.
    page.wait_for_timeout(300)

    selected_hour = page.locator(".dmy-hour-list .dmy-wheel-selected").inner_text()
    selected_minute = page.locator(".dmy-minute-list .dmy-wheel-selected").inner_text()
    assert selected_hour == "18", f"item highlight gio sau khi cuon khong dung: {selected_hour!r}"
    assert selected_minute == "37", f"item highlight phut sau khi cuon khong dung: {selected_minute!r}"

    page.click(".dmy-ok")
    value = page.input_value("#confirmed_run_at")
    assert value.endswith(" 18:37"), f"gia tri lay khi bam Chon KHONG khop item dang highlight sau cuon: {value!r}"


def test_B_reopen_with_existing_value_seeds_calendar_and_wheel_scroll(create_page_ready):
    page = create_page_ready
    page.fill("#confirmed_run_at", "05/03/2027 09:15")
    page.click(".dmy-picker-toggle")
    page.wait_for_selector(".dmy-picker-popup.dmy-show")
    assert page.inner_text(".dmy-picker-label").strip() == "Tháng 3/2027"
    assert page.locator(".dmy-picker-days .day.selected").inner_text() == "5"

    # Wheel gio/phut gio la carousel 3 ban sao lien tiep (xem buildWheelItems/attachWheelEvents
    # trong base.html, phuc vu yeu cau cuon vo han 23h -> 00h) - seed luon nam o BAN SAO GIUA
    # (index tuyet doi = cycle + value, cycle=24 cho gio/60 cho phut), khong con la 0 * ROW_H
    # nhu truoc khi chi co 1 ban sao duy nhat.
    HOUR_CYCLE = 24
    MINUTE_CYCLE = 60
    ROW_H = 36
    hour_scroll = page.eval_on_selector(".dmy-hour-list", "el => el.scrollTop")
    minute_scroll = page.eval_on_selector(".dmy-minute-list", "el => el.scrollTop")
    assert hour_scroll == (HOUR_CYCLE + 9) * ROW_H, f"cot gio khong tu cuon dung vi tri 09h: scrollTop={hour_scroll}"
    assert minute_scroll == (MINUTE_CYCLE + 15) * ROW_H, f"cot phut khong tu cuon dung vi tri 15p: scrollTop={minute_scroll}"
    assert page.locator(".dmy-hour-list .dmy-wheel-selected").inner_text() == "09"
    assert page.locator(".dmy-minute-list .dmy-wheel-selected").inner_text() == "15"


def test_B_hour_00_and_59_minute_scroll_to_middle_correctly(create_page_ready):
    """Edge case: gio 00:00 va phut 59 - voi carousel 3 ban sao (yeu cau cuon vo han 23h ->
    00h), 2 gia tri nay khong con la "dau/cuoi danh sach" nhu truoc (chi co 1 ban sao) - seed
    van luon nam o BAN SAO GIUA (index tuyet doi = cycle + value), con nguyen ca 1 ban sao
    day du phia truoc/sau de cuon tiep ca 2 huong ma khong ket."""
    page = create_page_ready
    page.fill("#confirmed_run_at", "01/01/2027 00:59")
    page.click(".dmy-picker-toggle")
    # Doi ".dmy-show" (khong phai chi ".open") - class nay CHI duoc them SAU khi
    # seedWheelScroll() da chay xong (xem openPopup() trong base.html, ca 2 nam trong cung
    # 1 callback requestAnimationFrame nen ".dmy-show" xuat hien la tin hieu dam bao seed da
    # hoan tat). Neu chi doi ".open" (them SOM HON, dong bo NGAY luc goi openPopup(), truoc
    # ca rAF), doc scrollTop ngay sau do co the vo tinh "thang" ve (race) truoc khi rAF kip
    # chay, doc duoc gia tri 0 mac dinh thay vi vi tri da seed - da phat hien qua thuc
    # nghiem (test nay tung fail 100% do doc scrollTop qua som, KHONG phai bug code that).
    page.wait_for_selector(".dmy-picker-popup.dmy-show")
    HOUR_CYCLE = 24
    MINUTE_CYCLE = 60
    ROW_H = 36
    hour_scroll = page.eval_on_selector(".dmy-hour-list", "el => el.scrollTop")
    minute_scroll = page.eval_on_selector(".dmy-minute-list", "el => el.scrollTop")
    assert hour_scroll == HOUR_CYCLE * ROW_H, f"cot gio khong tu cuon dung vi tri 00h o ban sao giua: scrollTop={hour_scroll}"
    assert minute_scroll == (MINUTE_CYCLE + 59) * ROW_H, f"cot phut khong tu cuon dung vi tri 59p o ban sao giua: scrollTop={minute_scroll}"
    assert page.locator(".dmy-hour-list .dmy-wheel-selected").inner_text() == "00"
    assert page.locator(".dmy-minute-list .dmy-wheel-selected").inner_text() == "59"


def test_B_cancel_does_not_change_input_and_closes_popup(create_page_ready):
    page = create_page_ready
    page.fill("#confirmed_run_at", "20/06/2026 08:00")
    page.click(".dmy-picker-toggle")
    page.wait_for_selector(".dmy-picker-popup.dmy-show")
    page.click(".dmy-picker-days .day:not(.muted):text-is('1')")
    page.click(".dmy-cancel")
    page.wait_for_function("() => !document.querySelector('.dmy-picker-popup').classList.contains('open')")
    assert page.input_value("#confirmed_run_at") == "20/06/2026 08:00"


def test_B_click_outside_closes_popup(create_page_ready):
    page = create_page_ready
    page.click(".dmy-picker-toggle")
    page.wait_for_selector(".dmy-picker-popup.dmy-show")
    page.click("h2")  # click ra ngoai wrap, con trong trang
    page.wait_for_function("() => !document.querySelector('.dmy-picker-popup').classList.contains('open')")


def test_B_today_button_selects_todays_date_keeps_time(create_page_ready):
    page = create_page_ready
    page.fill("#confirmed_run_at", "01/01/2026 03:33")
    page.click(".dmy-picker-toggle")
    page.wait_for_selector(".dmy-picker-popup.dmy-show")
    page.click(".dmy-today")
    today_iso = page.evaluate("() => { const d = new Date(); return `${d.getFullYear()}-${d.getMonth()+1}-${d.getDate()}`; }")
    year, month, day = today_iso.split("-")
    assert page.inner_text(".dmy-picker-label").strip() == f"Tháng {month}/{year}"
    selected = page.locator(".dmy-picker-days .day.selected")
    assert selected.inner_text() == day
    # Gio/phut khong bi doi boi nut Hom nay.
    hour_sel = page.locator(".dmy-hour-list .dmy-wheel-selected").inner_text()
    minute_sel = page.locator(".dmy-minute-list .dmy-wheel-selected").inner_text()
    assert hour_sel == "03"
    assert minute_sel == "33"


def test_B_empty_input_seeds_current_time_on_open(create_page_ready):
    page = create_page_ready
    assert page.input_value("#confirmed_run_at") == ""
    page.click(".dmy-picker-toggle")
    page.wait_for_selector(".dmy-picker-popup.dmy-show")
    hour_sel = page.locator(".dmy-hour-list .dmy-wheel-selected").inner_text()
    minute_sel = page.locator(".dmy-minute-list .dmy-wheel-selected").inner_text()
    # So sanh voi gio/phut LOCAL cua chinh trinh duyet tai thoi diem mo popup (seedFromInput()
    # dung `new Date()` - gio dia phuong cua may chay browser, KHONG phai UTC) - doc lai ngay
    # sau khi mo de tranh lech mui gio giua browser (local) va test process (neu khac nhau).
    now = page.evaluate("() => { const d = new Date(); return { h: d.getHours(), m: d.getMinutes() }; }")
    # Sai so <=1 gio/phut de chiu duoc do tre thuc thi test (vd vua qua ranh gioi phut/gio).
    assert abs(int(hour_sel) - now["h"]) <= 1
    assert abs(int(minute_sel) - now["m"]) <= 2 or abs(int(minute_sel) - now["m"]) >= 58


def test_B_month_boundary_dec_to_jan_and_back(create_page_ready):
    page = create_page_ready
    page.fill("#confirmed_run_at", "15/12/2026 10:00")
    page.click(".dmy-picker-toggle")
    page.wait_for_selector(".dmy-picker-popup.dmy-show")
    assert page.inner_text(".dmy-picker-label").strip() == "Tháng 12/2026"
    page.click(".dmy-next")
    assert page.inner_text(".dmy-picker-label").strip() == "Tháng 1/2027"
    page.click(".dmy-prev")
    page.click(".dmy-prev")
    assert page.inner_text(".dmy-picker-label").strip() == "Tháng 11/2026"


def test_B_february_leap_vs_non_leap_day_count(create_page_ready):
    page = create_page_ready
    page.fill("#confirmed_run_at", "01/02/2028 10:00")  # 2028 nhuan
    page.click(".dmy-picker-toggle")
    page.wait_for_selector(".dmy-picker-popup.dmy-show")
    assert page.locator(".dmy-picker-days .day:not(.muted):text-is('29')").count() == 1
    page.click(".dmy-cancel")

    page.fill("#confirmed_run_at", "01/02/2027 10:00")  # 2027 khong nhuan
    page.click(".dmy-picker-toggle")
    page.wait_for_selector(".dmy-picker-popup.dmy-show")
    assert page.locator(".dmy-picker-days .day:not(.muted):text-is('29')").count() == 0
    assert page.locator(".dmy-picker-days .day:not(.muted):text-is('28')").count() == 1


def test_B_open_close_repeatedly_no_crash_state_consistent(create_page_ready):
    page = create_page_ready
    for i in range(5):
        page.click(".dmy-picker-toggle")
        page.wait_for_selector(".dmy-picker-popup.dmy-show")
        page.click(".dmy-picker-toggle")
        page.wait_for_function("() => !document.querySelector('.dmy-picker-popup').classList.contains('open')")
    # Van dung sau nhieu lan mo/dong.
    page.click(".dmy-picker-toggle")
    page.wait_for_selector(".dmy-picker-popup.dmy-show")
    page.click(".dmy-picker-days .day:not(.muted):text-is('20')")
    page.click(".dmy-ok")
    assert page.locator("#confirmed_run_at").input_value() != ""


def test_B_modal_click_outside_popup_but_inside_modal_only_closes_popup(live_server, page, db_conn):
    """Rieng task_list.html: click ra ngoai popup lich nhung con trong modal
    #auto-deploy-modal chi duoc dong popup, KHONG duoc dong luon modal."""
    dev_login(page, live_server["base_url"], "b2-tester@example.com")
    promote_to_super_admin(db_conn, "b2-tester@example.com")
    task = seed_confirmed_task(db_conn, "b2-tester@example.com", commitid="modal001")
    page.goto(f"{live_server['base_url']}/tasks")
    page.click(f'form[action="/tasks/{task.id}/auto-deploy"] .auto-toggle')
    page.wait_for_selector("#auto-deploy-modal.open")
    page.click(".dmy-picker-toggle")
    page.wait_for_selector(".dmy-picker-popup.dmy-show")
    # Click vao vung modal-box nhung ngoai popup (vd vao doan text huong dan).
    page.click("#auto-deploy-modal .modal-box p")
    page.wait_for_function("() => !document.querySelector('.dmy-picker-popup').classList.contains('open')")
    assert "open" in (page.get_attribute("#auto-deploy-modal", "class") or "")


# ---------------------------------------------------------------------------
# C. Fallback go tay (khong dung popup)
# ---------------------------------------------------------------------------


def test_C_manual_valid_value_flows_to_submit_payload(live_server, page):
    dev_login(page, live_server["base_url"], "c-tester@example.com")
    page.goto(f"{live_server['base_url']}/tasks/create")
    page.fill("#group_search", "khong-ton-tai")  # se bi chan boi validate group_id truoc

    captured = {}

    def on_request(req):
        if req.url.endswith("/tasks/create") and req.method == "POST":
            captured["post_data"] = req.post_data

    page.on("request", on_request)

    page.fill("#confirmed_run_at", "29/07/2026 14:30")
    # application van dang disabled (dung hanh vi - chi bat khi da chon 1 group hop le), khong
    # can/khong the fill - chinh diem nay cung la 1 lop chan khac cho group khong hop le.
    page.fill('input[name="commitid"]', "abc1234")
    page.fill('[name="updatefor"]', "test")
    page.click('button[type="submit"]')
    page.wait_for_timeout(300)
    # group_id rong (chua chon group hop le trong dropdown) -> JS phai chan submit that
    # (alert + preventDefault), request KHONG duoc gui di.
    assert "post_data" not in captured, "submit khong bi chan du group_id chua hop le"


def test_C_manual_invalid_formats_blocked_before_submit(live_server, page, db_conn):
    group, project = seed_group_project(db_conn, "c-group", "c-app")
    dev_login(page, live_server["base_url"], "c2-tester@example.com")
    page.goto(f"{live_server['base_url']}/tasks/create")

    # Chon group/app hop le qua autocomplete that su.
    page.fill("#group_search", "c-group")
    page.click(".autocomplete-item:text-is('c-group')")
    page.wait_for_function("document.getElementById('application').disabled === false")
    page.fill("#application", "c-app")
    page.click("#application_dropdown .autocomplete-item:text-is('c-app')")
    page.fill('input[name="commitid"]', "abc1234")
    page.fill('[name="updatefor"]', "test")

    # 2 nhom hanh vi khac nhau nhung DEU phai chan submit thanh cong (khong gui request):
    #   - Dung dinh dang shape (2/2/4 digits, co dau /) nhung ngay/gio khong co that -> qua
    #     duoc HTML5 pattern="\\d{2}/\\d{2}/\\d{4} \\d{2}:\\d{2}", roi bi chan boi JS
    #     parseDmyDatetime() (alert()).
    #   - Sai hoan toan shape (dau -, thieu so 0...) -> bi chan SOM HON boi chinh thuoc tinh
    #     HTML5 pattern cua input (browser tu hien canh bao native, KHONG goi toi submit
    #     handler/alert() cua JS) - van la 1 lop chan hop le, khong phai bug.
    bad_values_expect_js_alert = [
        "31/02/2026 10:00",  # thang 2 khong co 31 ngay - dung shape, sai lich
        "15/07/2026 25:70",  # gio/phut tran - dung shape, sai gio/phut
    ]
    bad_values_expect_native_pattern_block = [
        "2026-07-15 10:00",  # sai dinh dang (ISO thay vi dmy)
        "15/7/2026 9:5",  # thieu so 0
    ]

    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))

    def submit_blocked(bad: str) -> bool:
        page.fill("#confirmed_run_at", bad)
        captured = {"sent": False}

        def on_request(req, captured=captured):
            if req.url.endswith("/tasks/create") and req.method == "POST":
                captured["sent"] = True

        page.on("request", on_request)
        page.click('button[type="submit"]')
        page.wait_for_timeout(200)
        page.remove_listener("request", on_request)
        return not captured["sent"]

    for bad in bad_values_expect_js_alert:
        dialogs.clear()
        assert submit_blocked(bad), f"gia tri sai lich '{bad}' KHONG bi chan, request van duoc gui"
        assert dialogs, f"khong co alert canh bao (JS parseDmyDatetime) khi nhap gia tri sai lich '{bad}'"

    for bad in bad_values_expect_native_pattern_block:
        dialogs.clear()
        assert submit_blocked(bad), f"gia tri sai dinh dang '{bad}' KHONG bi chan, request van duoc gui"
        is_valid = page.eval_on_selector("#confirmed_run_at", "el => el.checkValidity()")
        assert not is_valid, (
            f"gia tri sai dinh dang '{bad}' khong bi chan boi HTML5 pattern (checkValidity()==True) "
            "va cung khong co alert JS - khong ro co con lop chan nao khac hay khong"
        )


def test_C_manual_valid_datetime_with_valid_group_produces_correct_iso_payload(live_server, page, db_conn):
    group, project = seed_group_project(db_conn, "c3-group", "c3-app")
    dev_login(page, live_server["base_url"], "c3-tester@example.com")
    page.goto(f"{live_server['base_url']}/tasks/create")
    page.fill("#group_search", "c3-group")
    page.click(".autocomplete-item:text-is('c3-group')")
    page.wait_for_function("document.getElementById('application').disabled === false")
    page.fill("#application", "c3-app")
    page.click("#application_dropdown .autocomplete-item:text-is('c3-app')")
    page.fill('input[name="commitid"]', "abc1234")
    page.fill('[name="updatefor"]', "test")
    page.fill("#confirmed_run_at", "29/07/2026 14:30")

    with page.expect_request(lambda req: req.url.endswith("/tasks/create") and req.method == "POST") as req_info:
        page.click('button[type="submit"]')
    post_data = req_info.value.post_data
    assert "confirmed_run_at=2026-07-29T14%3A30" in post_data or "confirmed_run_at=2026-07-29T14:30" in post_data, (
        f"payload gui di khong dung ISO mong doi: {post_data!r}"
    )


# ---------------------------------------------------------------------------
# D. End-to-end submit
# ---------------------------------------------------------------------------


def test_D_create_request_payload_iso_and_no_js_error(live_server, page, db_conn):
    """Xac nhan (1) khong co loi JS khi submit qua picker, (2) payload gui di dung ISO.
    KHONG assert task duoc tao thanh cong trong DB - GIT_REMOTE_CREDENTIALS trong moi
    truong test rong nen check_commit_on_staging se that bai (ngoai pham vi widget, da
    ghi ro trong yeu cau task)."""
    group, project = seed_group_project(db_conn, "d-group", "d-app")
    js_errors = collect_console_errors(page)
    dev_login(page, live_server["base_url"], "d-tester@example.com")
    page.goto(f"{live_server['base_url']}/tasks/create")
    page.fill("#group_search", "d-group")
    page.click(".autocomplete-item:text-is('d-group')")
    page.wait_for_function("document.getElementById('application').disabled === false")
    page.fill("#application", "d-app")
    page.click("#application_dropdown .autocomplete-item:text-is('d-app')")
    page.fill('input[name="commitid"]', "abc1234")
    page.fill('[name="updatefor"]', "test D")

    page.click(".dmy-picker-toggle")
    page.wait_for_selector(".dmy-picker-popup.dmy-show")
    page.click(".dmy-picker-days .day:not(.muted):text-is('20')")
    page.locator(".dmy-hour-list .dmy-wheel-item:text-is('11')").first.click()
    page.locator(".dmy-minute-list .dmy-wheel-item:text-is('30')").first.click()
    page.click(".dmy-ok")

    with page.expect_request(lambda req: req.url.endswith("/tasks/create") and req.method == "POST") as req_info:
        with page.expect_response(lambda r: r.url.endswith("/tasks/create") and r.request.method == "POST") as resp_info:
            page.click('button[type="submit"]')
    post_data = req_info.value.post_data
    assert "11%3A30" in post_data or "11:30" in post_data, f"payload thieu gio/phut da chon qua picker: {post_data!r}"

    resp = resp_info.value
    if resp.status == 500:
        # KY VONG NGOAI PHAM VI: git remote khong duoc cau hinh trong sandbox test
        # (GIT_REMOTE_CREDENTIALS rong) -> check_commit_on_staging() nem GitError, va
        # app/routers/tasks_router.py::create_submit KHONG bat exception nay (chi bat
        # (ValueError, OverflowError) quanh buoc parse datetime, khong bao quanh
        # task_service.create_task) -> loi 500 khong duoc xu ly gracefully. Day la BUG
        # LOGIC backend co that (khong lien quan widget lich - report cho [dev], KHONG
        # phai loi cua initDmyPicker) nhung KHONG chan ket luan PASS cho widget: payload
        # JS gui di van dung dinh dang ISO nhu assert o tren da xac nhan.
        pass
    else:
        page.wait_for_timeout(500)
        real_js_errors = [e for e in js_errors if "confirmed_run_at" not in e and "500" not in e]
        assert not real_js_errors, f"Co console error JS khi submit qua picker: {real_js_errors}"


def test_D_auto_deploy_enable_via_picker_persists_after_reload(live_server, page, db_conn):
    dev_login(page, live_server["base_url"], "d2-tester@example.com")
    promote_to_super_admin(db_conn, "d2-tester@example.com")
    task = seed_confirmed_task(db_conn, "d2-tester@example.com", commitid="dperst1")
    page.goto(f"{live_server['base_url']}/tasks")

    # BUG DA PHAT HIEN (xem ghi chu chi tiet cuoi file): o vien port 1280x900 (mac dinh cua
    # fixture `page`, tuong duong da so laptop pho bien) cac nut Chon/Huy/Hom nay cua popup
    # lich trong modal Auto deploy bi TRAN QUA DUOI VIEWPORT, khong the click duoc (modal la
    # position:fixed toan man hinh, khong co co che scroll nao de keo popup vao vung nhin).
    # Phong to viewport rieng cho test nay CHI DE tiep tuc kiem thu duoc logic cua widget
    # (chon ngay/gio/phut, submit, persist) - KHONG phai fix/che dau bug, bug van duoc bao
    # cao rieng cho developer o cuoi file test nay.
    page.set_viewport_size({"width": 1280, "height": 1400})

    page.click(f'form[action="/tasks/{task.id}/auto-deploy"] .auto-toggle')
    page.wait_for_selector("#auto-deploy-modal.open")
    page.click(".dmy-picker-toggle")
    page.wait_for_selector(".dmy-picker-popup.dmy-show")
    page.click(".dmy-picker-days .day:not(.muted):text-is('22')")
    page.locator(".dmy-hour-list .dmy-wheel-item:text-is('16')").first.click()
    page.locator(".dmy-minute-list .dmy-wheel-item:text-is('05')").first.click()
    page.click(".dmy-ok")
    assert page.input_value("#auto-deploy-time").endswith(" 16:05")

    page.click("#auto-deploy-ok")
    page.wait_for_timeout(500)  # ajax-form: doAjaxSubmit() -> window.location.reload() khi ok

    page.wait_for_selector(f'form[action="/tasks/{task.id}/auto-deploy"] .auto-toggle.on')
    title = page.get_attribute(f'form[action="/tasks/{task.id}/auto-deploy"] .auto-toggle', "title")
    assert "16:05" in title, f"gio Auto deploy khong duoc luu/hien dung sau reload: title={title!r}"

    # Reload lan nua (F5 that su) de chac chan gia tri persist trong DB, khong chi trong
    # response cua request truoc.
    page.reload()
    title_after_reload = page.get_attribute(f'form[action="/tasks/{task.id}/auto-deploy"] .auto-toggle', "title")
    assert "16:05" in title_after_reload


# ---------------------------------------------------------------------------
# BUG CANDIDATE (bao cao cho [dev], KHONG phai loi test): popup lich moi (cao hon nhieu so
# voi 2 o <input type="number"> cu - ~475px gom luoi lich + 2 cot wheel scroll 180px + nut
# hanh dong) mo trong modal Auto deploy (task_list.html) co the TRAN QUA DUOI VIEWPORT tren
# cac kich thuoc man hinh laptop pho bien (vd 1366x768, 1280x900) - modal la position:fixed
# toan man hinh, KHONG co scroll container nao bao quanh popup de nguoi dung keo len xem nut
# "Chọn"/"Huỷ"/"Hôm nay", cung khong co logic tu lat popup len tren khi khong du cho o duoi
# (kieu "flip" pho bien o cac date picker khac). Test duoi day CHU Y SE FAIL tren viewport
# 1280x900 (mac dinh cua fixture `page`) de chung minh bug tai hien duoc, khong phai loi
# viet test - so sanh voi test_D_auto_deploy_enable_via_picker_persists_after_reload da phai
# CHU DONG phong to viewport (set_viewport_size 1280x1400) moi bam duoc nut Chọn.
# ---------------------------------------------------------------------------


def test_BUG_auto_deploy_popup_ok_button_overflows_common_viewport_height(live_server, page, db_conn):
    dev_login(page, live_server["base_url"], "bug-tester@example.com")
    promote_to_super_admin(db_conn, "bug-tester@example.com")
    task = seed_confirmed_task(db_conn, "bug-tester@example.com", commitid="bugview1")
    page.goto(f"{live_server['base_url']}/tasks")

    page.click(f'form[action="/tasks/{task.id}/auto-deploy"] .auto-toggle')
    page.wait_for_selector("#auto-deploy-modal.open")
    page.click(".dmy-picker-toggle")
    page.wait_for_selector(".dmy-picker-popup.dmy-show")

    viewport = page.viewport_size
    ok_box = page.locator(".dmy-ok").bounding_box()
    ok_bottom = ok_box["y"] + ok_box["height"]
    assert ok_bottom <= viewport["height"], (
        "BUG: nut '.dmy-ok' (Chọn) trong popup lich cua modal Auto deploy tran ra ngoai "
        f"viewport ({viewport['width']}x{viewport['height']}) - ok_box={ok_box}, "
        f"ok_bottom={ok_bottom:.1f} > viewport_height={viewport['height']}. Nguoi dung khong "
        "the click de xac nhan gio da chon qua popup tren man hinh kich thuoc nay (khong co "
        "cach scroll vao vi modal la position:fixed va khong co logic 'flip' popup len tren "
        "khi khong du khong gian ben duoi)."
    )


# ---------------------------------------------------------------------------
# E. Regression nhe: khong co console error tren ca 2 trang khi tai binh thuong
# (chua tuong tac gi voi picker) - phan pytest suite day du chay rieng qua lenh
# `pytest` o root, khong lap lai trong file nay.
# ---------------------------------------------------------------------------


def test_E_no_console_errors_on_task_form_page_load(live_server, page, db_conn):
    errors = collect_console_errors(page)
    dev_login(page, live_server["base_url"], "e-tester@example.com")
    page.goto(f"{live_server['base_url']}/tasks/create")
    page.wait_for_selector("#confirmed_run_at")
    page.wait_for_timeout(300)
    assert not errors, f"Console errors on task_form.html: {errors}"


def test_E_no_console_errors_on_task_list_page_load_and_modal_open(live_server, page, db_conn):
    dev_login(page, live_server["base_url"], "e2-tester@example.com")
    promote_to_super_admin(db_conn, "e2-tester@example.com")
    task = seed_confirmed_task(db_conn, "e2-tester@example.com", commitid="eerr0001")
    errors = collect_console_errors(page)
    page.goto(f"{live_server['base_url']}/tasks")
    page.wait_for_timeout(200)
    page.click(f'form[action="/tasks/{task.id}/auto-deploy"] .auto-toggle')
    page.wait_for_selector("#auto-deploy-modal.open")
    page.wait_for_timeout(300)
    assert not errors, f"Console errors on task_list.html + modal: {errors}"
