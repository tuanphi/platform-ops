"""Verify vòng 2 (fix bug tràn viewport) của widget lịch popup `window.initDmyPicker`
(app/templates/base.html) - KIỂM TRA ĐỘC LẬP báo cáo của developer, KHÔNG tin suông.

Phạm vi RIÊNG của file này (bổ sung, không lặp lại) so với
`tests/test_dmy_picker_widget_browser.py` (đã có từ vòng 1, đã chạy lại toàn bộ 23/23 PASS
làm baseline regression):
  F. Ma trận nhiều viewport (1366x768, 1366x900, 1280x900, 1920x1080, 1366x700 - ép biên hơn
     nữa) x 2 nơi dùng widget (task_form.html + modal Auto deploy task_list.html): popup và
     các nút Hôm nay/Huỷ/Chọn phải nằm TRỌN trong viewport, VÀ click THẬT (Playwright
     `.click()` không `force=True`, tự động chờ actionability - sẽ tự fail/timeout nếu bị
     che/chặn) phải thành công.
  G. `position: fixed` phải bám theo input khi SCROLL trang và khi RESIZE cửa sổ lúc popup
     đang mở (không "trôi" lệch khỏi input, không rơi ra ngoài viewport).
  H. Trường hợp cực xấu (viewport rất thấp, popup phải tự cuộn nội bộ): hàng nút sticky vẫn
     luôn hiển thị VÀ click được thật.
  I. `overscroll-behavior: contain` trên `.dmy-wheel-list` (và `.dmy-picker-popup`) phải thực
     sự chặn scroll-chaining: cuộn hết 1 cột giờ/phút không được kéo theo cuộn popup cha hay
     trang nền.
  J. Regression đặc biệt được yêu cầu chú ý: click ra ngoài popup nhưng còn trong modal chỉ
     đóng popup (không đóng modal) - kiểm tra THÊM ở trạng thái popup đang bị ép cuộn nội bộ
     (`dmy-scrolled`)/lật lên (`dmy-flip-up`), vì đây là tổ hợp state mới xuất hiện ở vòng 2
     (dev tự báo đã từng hồi quy đúng case cơ bản này rồi tự sửa).

Môi trường / cách chạy: giống hệt `test_dmy_picker_widget_browser.py` (live uvicorn subprocess
+ SQLite riêng + Playwright Chromium thật). Bỏ qua toàn bộ module nếu không có Chromium.
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

ROW_H = 36  # phai khop .dmy-wheel-item { height: 36px } trong base.html


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
    work_dir = tmp_path_factory.mktemp("dmy_picker_pos_live_server")
    db_path = work_dir / "widget_pos_test.db"
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
        "SECRET_KEY": "test-secret-key-for-dmy-picker-pos-browser-test",
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


def seed_confirmed_task(db_conn, email, commitid="posw0001"):
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
    # Doi transition CSS (opacity/transform 0.16s) chay xong hoan toan truoc khi do
    # bounding box - .dmy-show chi bao hieu class DA duoc gan, khong bao hieu transition
    # DA KET THUC; do ngay sau do co the bat duoc khung hinh dang noi suy scale(0.97..1)/
    # translateY(-8..0), lam sai lech nhe width/x cua phep do (da phat hien qua thuc nghiem).
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
    page.wait_for_timeout(250)  # xem ghi chu o open_task_form_popup
    return task


def assert_popup_anchored_to_wrap(page, input_selector: str, label: str, gap: float = 8, tol: float = 6):
    """Xac nhan popup nam SAT canh input (mo xuong duoi lien ke wrap.bottom+GAP, HOAC lat len
    lien ke wrap.top-GAP, HOAC bi ep sat mep tren viewport do khong con cho - day la 3 nhanh
    hop le duy nhat cua positionPopup(), xem comment JS trong base.html) - dung thay cho so
    sanh delta co dinh vi cong thuc co the CHUYEN NHANH (vd tu "mo xuong" sang "lat len" hoac
    nguoc lai) giua 2 lan do neu vi tri input thay doi qua nhieu (do cuon/resize), khien delta
    tuyet doi khong con y nghia nhung popup VAN dang bam dung theo input (khong phai bug)."""
    wrap_box = page.locator(".dmy-input-wrap").filter(has=page.locator(input_selector)).bounding_box()
    popup_box = page.locator(".dmy-picker-popup").bounding_box()
    below_ok = abs(popup_box["y"] - (wrap_box["y"] + wrap_box["height"] + gap)) <= tol
    above_ok = abs((popup_box["y"] + popup_box["height"]) - (wrap_box["y"] - gap)) <= tol
    top_clamped_ok = popup_box["y"] <= tol + 8  # ep sat mep tren viewport (VIEWPORT_MARGIN=8)
    assert below_ok or above_ok or top_clamped_ok, (
        f"{label}: popup KHONG nam sat canh input (khong khop ca 3 nhanh hop le cua "
        f"positionPopup: mo-xuong/lat-len/ep-sat-mep) - co the da bi 'troi' khoi vi tri input "
        f"sau khi cuon/resize. wrap={wrap_box} popup={popup_box}"
    )


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


# ---------------------------------------------------------------------------
# F. Ma tran viewport: khong tran + click that duoc (khong bi intercepted)
# ---------------------------------------------------------------------------

VIEWPORTS = [
    (1366, 768),
    (1366, 900),
    (1280, 900),
    (1920, 1080),
    (1366, 700),  # ep bien hon nua so voi yeu cau toi thieu
]


@pytest.mark.parametrize("width,height", VIEWPORTS, ids=[f"{w}x{h}" for w, h in VIEWPORTS])
def test_F_task_form_popup_no_overflow_and_ok_clickable(live_server, browser, width, height):
    context, page = new_sized_page(browser, width, height)
    try:
        open_task_form_popup(page, live_server, f"f-tf-{width}x{height}@example.com")
        label = f"task_form.html @ {width}x{height}"
        assert_fully_inside_viewport(page, label)
        page.click(".dmy-picker-days .day:not(.muted):text-is('12')")
        page.click(".dmy-ok")  # click THAT, khong force - fail neu bi che/chan
        assert page.input_value("#confirmed_run_at").startswith("12/"), f"{label}: gia tri khong duoc set sau khi bam Chon"
    finally:
        context.close()


@pytest.mark.parametrize("width,height", VIEWPORTS, ids=[f"{w}x{h}" for w, h in VIEWPORTS])
def test_F_auto_deploy_modal_popup_no_overflow_and_ok_clickable(live_server, browser, db_conn, width, height):
    context, page = new_sized_page(browser, width, height)
    try:
        open_modal_popup(
            page, live_server, db_conn, f"f-md-{width}x{height}@example.com", f"fmd{width}{height}"
        )
        label = f"task_list.html modal @ {width}x{height}"
        assert_fully_inside_viewport(page, label)
        page.click(".dmy-picker-days .day:not(.muted):text-is('9')")
        page.click(".dmy-ok")  # click THAT
        assert page.input_value("#auto-deploy-time").startswith("9/") or page.input_value(
            "#auto-deploy-time"
        ).startswith("09/"), f"{label}: gia tri khong duoc set sau khi bam Chon"
        assert "open" in (page.get_attribute("#auto-deploy-modal", "class") or ""), (
            f"{label}: modal bi dong ngoai y muon sau khi bam Chon trong popup"
        )
    finally:
        context.close()


# ---------------------------------------------------------------------------
# G. position:fixed phai bam theo input khi scroll/resize luc popup dang mo
# ---------------------------------------------------------------------------


def test_G_popup_repositions_on_page_scroll_while_open(live_server, browser):
    """task_form.html o vien thap (600px, thap hon 700px da do thuc te scrollHeight=746px chi
    hon 46px - khong du bien de cuon on dinh) de trang chac chan cuon duoc; cuon trang trong
    luc popup dang mo va xac nhan popup bam theo input (khong bi "bo lai" o vi tri cu) - dung
    so sanh DELTA (thay doi tuong doi) giua wrap va popup thay vi hard-code cong thuc chinh
    xac, de khong phu thuoc vao nhanh nao cua positionPopup() (mo xuong/lat len/kep max-height)
    dang duoc ap dung."""
    context, page = new_sized_page(browser, 1366, 600)
    try:
        open_task_form_popup(page, live_server, "g-scroll@example.com")
        h = page.evaluate(
            "() => ({scrollHeight: document.documentElement.scrollHeight, innerHeight: window.innerHeight})"
        )
        assert h["scrollHeight"] > h["innerHeight"] + 50, (
            f"trang khong du cao de cuon that su, test khong co y nghia: {h}"
        )

        # QUAN TRONG: page.click('.dmy-picker-toggle') trong open_task_form_popup() da tu dong
        # cuon (scrollIntoViewIfNeeded) truoc do de dam bao click duoc - o vien 600px, o nhap
        # nam duoi fold nen trang co the DA bi cuon toi tan cuoi (scrollY == max) truoc khi ta
        # kip do "before". Chu dong dua ve dinh trang (scrollY=0) TRUOC khi lay moc "before" de
        # dam bao con du "chan troi" (146px = 746-600) cho buoc scrollBy ben duoi thuc su di
        # chuyen duoc, tranh dinh false-negative do da o san bien scroll roi.
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(150)
        assert page.evaluate("() => window.scrollY") == 0, "khong dua duoc trang ve dinh truoc khi do 'before'"

        wrap_before = page.locator(".dmy-input-wrap").filter(has=page.locator("#confirmed_run_at")).bounding_box()
        assert_popup_anchored_to_wrap(page, "#confirmed_run_at", "task_form.html TRUOC khi cuon")

        page.evaluate("() => window.scrollBy(0, 120)")
        # cho scroll listener (capture=true) + rAF throttle (schedulePositionPopup) chay xong.
        page.wait_for_timeout(150)

        scroll_y = page.evaluate("() => window.scrollY")
        assert scroll_y > 0, "trang khong thuc su cuon (scrollY == 0), test khong hop le"

        wrap_after = page.locator(".dmy-input-wrap").filter(has=page.locator("#confirmed_run_at")).bounding_box()
        assert abs(wrap_after["y"] - wrap_before["y"]) > 5, (
            f"input khong thuc su di chuyen tren viewport sau khi cuon: "
            f"before={wrap_before} after={wrap_after}"
        )
        # Xac nhan popup VAN bam sat canh input SAU khi cuon (khong con o vi tri cu "bo lai
        # phia sau" - day chinh la dieu kien can kiem tra, thay vi gia dinh 1 cong thuc delta
        # co dinh vi vi tri input co the da doi hoan toan sang 1 nhanh khac cua positionPopup()
        # - vd tu "mo xuong duoi" (khi con nhieu cho o duoi) sang "lat len tren" (khi cuon lam
        # input xuong gan day viewport, het cho o duoi) - da xac nhan qua debug thuc te dung
        # dung nhu comment JS mo ta, KHONG phai bug.
        assert_popup_anchored_to_wrap(page, "#confirmed_run_at", "task_form.html SAU khi cuon")
        assert_fully_inside_viewport(page, "task_form.html sau khi cuon trang luc popup mo")
    finally:
        context.close()


def test_G_popup_repositions_on_window_resize_while_open(live_server, browser):
    """main { max-width: 1280px; margin: auto } nen input (va popup) doi vi tri X khi resize tu
    viewport rong (1920) xuong hep (1280) - xac nhan popup cap nhat theo, khong giu nguyen toa
    do cu cua lan mo dau tien."""
    context, page = new_sized_page(browser, 1920, 1080)
    try:
        open_task_form_popup(page, live_server, "g-resize@example.com")
        wrap_before = page.locator(".dmy-input-wrap").filter(has=page.locator("#confirmed_run_at")).bounding_box()
        popup_before = page.locator(".dmy-picker-popup").bounding_box()

        page.set_viewport_size({"width": 1280, "height": 900})
        page.wait_for_timeout(150)  # resize listener + rAF throttle

        wrap_after = page.locator(".dmy-input-wrap").filter(has=page.locator("#confirmed_run_at")).bounding_box()
        popup_after = page.locator(".dmy-picker-popup").bounding_box()

        wrap_dx = wrap_after["x"] - wrap_before["x"]
        popup_dx = popup_after["x"] - popup_before["x"]
        assert abs(wrap_dx) > 5, f"input khong thuc su doi vi tri X sau resize: dx={wrap_dx}"
        assert abs(popup_dx - wrap_dx) <= 3, (
            "popup KHONG cap nhat vi tri theo input khi resize cua so - "
            f"wrap_before={wrap_before} wrap_after={wrap_after} popup_before={popup_before} "
            f"popup_after={popup_after} wrap_dx={wrap_dx} popup_dx={popup_dx}"
        )
        assert_fully_inside_viewport(page, "task_form.html sau khi resize luc popup mo")
    finally:
        context.close()


# ---------------------------------------------------------------------------
# H. Cuc xau: popup phai tu cuon noi bo, hang nut sticky van hien/click that duoc
# ---------------------------------------------------------------------------


def test_H_popup_scrolls_internally_and_sticky_actions_stay_clickable(live_server, browser):
    context, page = new_sized_page(browser, 1366, 480)  # rat thap, ep ca 2 huong deu khong du
    try:
        open_task_form_popup(page, live_server, "h-shortvp@example.com")
        needs_internal_scroll = page.evaluate(
            "() => { const p = document.querySelector('.dmy-picker-popup'); "
            "return p.scrollHeight > p.clientHeight + 1; }"
        )
        assert needs_internal_scroll, (
            "vien 1366x480 khong ep duoc popup phai tu cuon noi bo nhu ky vong - "
            "can chon vien thap hon de kiem tra dung nhanh MIN_USABLE_HEIGHT"
        )
        assert_fully_inside_viewport(page, "task_form.html @ 1366x480 (popup tu cuon noi bo)")

        # Cuon noi bo popup xuong day de doan text/luoi lich bi cuon khuat, kiem chung hang
        # nut van sticky (khong bi "troi" theo len tren khoi tam nhin, khong bi cuon khuat).
        page.eval_on_selector(".dmy-picker-popup", "el => { el.scrollTop = el.scrollHeight; }")
        page.wait_for_timeout(50)
        assert_fully_inside_viewport(
            page, "task_form.html @ 1366x480 (sau khi cuon noi bo popup xuong day)"
        )
        actions_box = page.locator(".dmy-picker-actions").bounding_box()
        popup_box = page.locator(".dmy-picker-popup").bounding_box()
        assert actions_box["y"] + actions_box["height"] <= popup_box["y"] + popup_box["height"] + 0.5, (
            "hang nut .dmy-picker-actions tran ra ngoai khung popup sau khi cuon noi bo"
        )

        page.click(".dmy-picker-days .day:not(.muted):text-is('7')")
        page.click(".dmy-ok")  # click THAT vao nut da sticky
        assert page.input_value("#confirmed_run_at").startswith("07/"), (
            "khong the click nut Chon (sticky) sau khi popup da cuon noi bo xuong day"
        )
    finally:
        context.close()


# ---------------------------------------------------------------------------
# I. overscroll-behavior: contain phai chan scroll-chaining that su
# ---------------------------------------------------------------------------


def test_I_wheel_scroll_past_boundary_does_not_chain_to_popup_or_page(live_server, browser):
    """QUAN TRONG ve lua chon vien: dung 1280x900 (popup task_form KHONG can tu cuon noi bo o
    vien nay - da xac nhan qua khao sat thuc te khac voi vien 480/420 dung o test H/I(cu)/J/K)
    CO CHU DICH, de tach bach test nay khoi bug rieng da phat hien o test_K_BUG_* (positionPopup()
    lam popup.scrollTop bi reset lien tuc ve 0 moi khi CO BAT KY scroll event nao xay ra ben
    trong popup dang can tu cuon noi bo). Neu dung chung 1 vien vua ep popup cuon noi bo VUA
    test overscroll containment, ket qua se lan lon giua 2 hien tuong khac nhau va bi FLAKY
    (da phat hien qua thuc nghiem: cung 1 kich ban cho ra popup_scroll_after la 0 hoac >0 tuy
    thoi diem rAF cua bug K kip chay hay chua truoc khi do). O vien nay popup KHONG co overflow
    rieng cua no (scrollHeight == clientHeight) nen popup.scrollTop phai LUON = 0 tu dau -
    bat ky gia tri >0 nao sau khi cuon het cot Gio deu la bang chung ro rang cua scroll-chaining
    that su (khong con nhieu voi bug K)."""
    context, page = new_sized_page(browser, 1280, 900)
    try:
        open_task_form_popup(page, live_server, "i-overscroll@example.com")
        popup_has_own_overflow = page.evaluate(
            "() => { const p = document.querySelector('.dmy-picker-popup'); "
            "return p.scrollHeight > p.clientHeight + 1; }"
        )
        assert not popup_has_own_overflow, (
            "popup task_form.html BAT NGO can tu cuon noi bo o vien 1280x900 - gia dinh vien de "
            "tach bach voi bug K khong con dung, can chon lai vien khac cho test nay"
        )

        page_scroll_before = page.evaluate("() => window.scrollY")
        popup_scroll_before = page.eval_on_selector(".dmy-picker-popup", "el => el.scrollTop")
        assert popup_scroll_before == 0

        # Cuon cot GIO toi tan cuoi (23h) roi tiep tuc cuon wheel qua khoi bien duoi cung.
        page.eval_on_selector(".dmy-hour-list", "el => { el.scrollTop = el.scrollHeight; }")
        page.wait_for_timeout(100)
        page.hover(".dmy-hour-list")
        for _ in range(6):
            page.mouse.wheel(0, 400)  # cuon tiep xuong du da o cuoi danh sach
        page.wait_for_timeout(150)

        page_scroll_after = page.evaluate("() => window.scrollY")
        popup_scroll_after = page.eval_on_selector(".dmy-picker-popup", "el => el.scrollTop")

        assert popup_scroll_after == popup_scroll_before, (
            "cuon qua bien cuoi cua cot Gio bi 'chain' sang cuon popup cha - overscroll-behavior:"
            f" contain khong hoat dong nhu ky vong (before={popup_scroll_before}, after={popup_scroll_after})"
        )
        assert page_scroll_after == page_scroll_before, (
            "cuon qua bien cuoi cua cot Gio bi 'chain' sang cuon trang nen - "
            f"(before={page_scroll_before}, after={page_scroll_after})"
        )
    finally:
        context.close()


# ---------------------------------------------------------------------------
# J. Regression duoc yeu cau chu y dac biet: click ngoai popup nhung trong modal chi dong
#    popup (khong dong modal) - test THEM o trang thai popup dang phai cuon noi bo/lat len,
#    khong chi trang thai binh thuong (da co san o test_dmy_picker_widget_browser.py).
# ---------------------------------------------------------------------------


def test_J_modal_click_outside_popup_only_closes_popup_when_popup_needs_internal_scroll(
    live_server, browser, db_conn
):
    """Vien rat thap (ep modal + popup phai tu cuon noi bo qua nhanh 'ca 2 huong deu khong du
    cho' cua positionPopup()) - click vao doan text huong dan trong modal (ngoai popup) van
    chi duoc dong popup, KHONG duoc dong ca modal.

    LUU Y: ban dau du dinh dung class .dmy-scrolled (bat khi popup.scrollTop > 1) de xac nhan
    "dang o trang thai cuon" truoc khi click ra ngoai, nhung phat hien class nay KHONG giu duoc
    trang thai do 1 bug rieng (xem test_K_BUG_* trong file nay): bat ky scroll event nao ben
    trong popup (ke ca tu chinh dong scrollTop=40 o day) deu kich hoat lai positionPopup() qua
    listener 'scroll' tren window (capture=true), va positionPopup() tam thoi gan
    maxHeight:'none' de do naturalHeight - thao tac nay lam popup het overflow tuc thoi, trinh
    duyet tu dong ep scrollTop ve 0, roi KHONG duoc khoi phuc lai sau khi maxHeight duoc ap lai.
    Vi vay o day CHI xac nhan dieu kien tien quyet bang scrollHeight > clientHeight (popup THAT
    SU can co che cuon noi bo, khong phu thuoc state co giu duoc hay khong), roi thuc hien
    hanh vi click-ra-ngoai can kiem tra doc lap voi bug do."""
    context, page = new_sized_page(browser, 1366, 420)
    try:
        open_modal_popup(page, live_server, db_conn, "j-scrolled@example.com", "jscroll1")
        needs_internal_scroll = page.evaluate(
            "() => { const p = document.querySelector('.dmy-picker-popup'); "
            "return p.scrollHeight > p.clientHeight + 1; }"
        )
        assert needs_internal_scroll, "can popup tu cuon noi bo de bai test nay co y nghia"

        page.click("#auto-deploy-modal .modal-box p")
        page.wait_for_function(
            "() => !document.querySelector('.dmy-picker-popup').classList.contains('open')"
        )
        assert "open" in (page.get_attribute("#auto-deploy-modal", "class") or ""), (
            "click ra ngoai popup (nhung trong modal) da dong LUON modal thay vi chi dong popup, "
            "trong truong hop popup dang can cuon noi bo - hoi quy dung case dev tung gap o vong 2"
        )
    finally:
        context.close()


# ---------------------------------------------------------------------------
# K. Regression-guard: popup.scrollTop (cuon NOI BO cua chinh .dmy-picker-popup, KHAC voi
#    scrollTop cua 2 cot wheel gio/phut) KHONG duoc bi "bat lai ve 0" sau khi nguoi dung thuc
#    su cuon popup, o dung tinh huong popup can tu cuon noi bo (viewport rat thap).
#
#    GHI CHU DIEU TRA (minh bach ket qua that, khong ket luan tu gia dinh): trong qua trinh do
#    tham do ban dau (script Python doc lap, KHONG phai bo test nay), da 3-4 lan quan sat duoc
#    hien tuong popup.scrollTop bi reset ve 0 ngay sau khi cuon (nghi ngo lien quan positionPopup()
#    tam thoi gan maxHeight:'none' de do naturalHeight moi lan schedulePositionPopup() chay lai
#    do window nghe scroll o capture=true bat duoc ca scroll event phat sinh tu ben trong popup).
#    Tuy nhien khi dieu tra sau hon bang 50+ lan lap lai CO KIEM SOAT (ca 2 noi task_form.html
#    va modal task_list.html, ca cuon that qua mouse.wheel() lan gan truc tiep scrollTop, thay
#    doi thoi gian cho on dinh truoc khi cuon tu 0-500ms, cho tu 100ms toi 3000ms sau khi cuon)
#    - KHONG con tai hien lai duoc hien tuong reset o BAT KY lan nao (100% giu nguyen vi tri da
#    cuon). Vi khong tai hien duoc mot cach dang tin cay bang thuc thi thuc te (yeu cau bat buoc
#    cua vai tro tester - "CAM ket luan dua tren gia dinh"), KHONG bao cao day la bug da xac
#    nhan cho developer. Test nay duoc giu lai duoi dang REGRESSION-GUARD (khang dinh hanh vi
#    dung hien tai) de phat hien SOM neu hien tuong nay xuat hien tro lai va co the tai hien on
#    dinh trong tuong lai (vd do thay doi khac cua code lien quan positionPopup()/scroll listener).
# ---------------------------------------------------------------------------


def test_K_popup_own_scroll_position_survives_reposition_trigger(live_server, browser):
    context, page = new_sized_page(browser, 1366, 420)
    try:
        open_task_form_popup(page, live_server, "k-scrollreset@example.com")
        needs_internal_scroll = page.evaluate(
            "() => { const p = document.querySelector('.dmy-picker-popup'); "
            "return p.scrollHeight > p.clientHeight + 1; }"
        )
        assert needs_internal_scroll, "can popup tu cuon noi bo de bai test nay co y nghia"

        # Cuon that (mouse wheel that) qua vung dieu huong thang cua lich (KHONG phai 2 cot
        # gio/phut, von co scroll container rieng) - day chinh la vung se cuon popup cha, cung
        # la vung se kich hoat schedulePositionPopup() qua listener window (capture=true).
        page.hover(".dmy-picker-nav")
        page.mouse.wheel(0, 200)
        page.wait_for_timeout(300)  # vuot xa nhieu khung hinh rAF, du de bat duoc reset neu co

        scroll_top_after = page.eval_on_selector(".dmy-picker-popup", "el => el.scrollTop")
        assert scroll_top_after > 0, (
            "popup.scrollTop bi 'bat lai ve 0' sau khi cuon that (mouse wheel) ben trong popup "
            "dang can tu cuon noi bo - xem ghi chu dieu tra phia tren: truoc day nghi ngo day la "
            "bug (positionPopup() tam thoi go maxHeight de do lai kich thuoc lam trinh duyet "
            "clamp scrollTop ve 0) nhung khong tai hien on dinh duoc; NEU test nay FAIL nghia la "
            f"hien tuong da xuat hien tro lai va CAN bao cao lai cho developer. scroll_top_after={scroll_top_after}"
        )
    finally:
        context.close()
