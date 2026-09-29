"""HAPPY PATH cho phan trang hien dai `/tasks` (PAGE_SIZE=10, xem
app/routers/tasks_router.py: _build_page_items + key page_items truyen vao template).

Khong dung lai code tinh nang, chi verify hanh vi tu ben ngoai (HTTP response) va
helper thuan Python _build_page_items o vai cap gia tri bien."""

from app.models import DeployTask, Role, TaskStatus
from app.routers.tasks_router import _build_page_items
from tests.conftest import make_user


def make_task(db_session, index, **kwargs):
    defaults = dict(
        project="core",
        application="api",
        commitid=f"c{index:04d}",
        status=TaskStatus.TASK,
        email="admin@example.com",
    )
    defaults.update(kwargs)
    task = DeployTask(**defaults)
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


def make_many_tasks(db_session, count, **kwargs):
    for i in range(count):
        make_task(db_session, i, **kwargs)


def test_first_page_has_pagination_block_and_prev_disabled(client_factory, db_session):
    """TC1: 25 task (PAGE_SIZE=10 -> 3 trang). GET /tasks?page=1 tra 200, co khoi
    #pagination, va nut Prev bi disable (khong phai the <a>)."""
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    make_many_tasks(db_session, 25)

    client = client_factory(user=admin)
    resp = client.get("/tasks?page=1")

    assert resp.status_code == 200
    assert 'id="pagination"' in resp.text
    # Prev disabled: span.page-btn.disabled xuat hien, KHONG co <a ...>Prev (aria-label
    # "Trang truoc") tro toi page=0.
    assert 'aria-label="Trang trước" aria-disabled="true"' in resp.text
    assert 'href="/tasks?page=0' not in resp.text
    # Next van con (con trang sau) va co link page=2. Luu y: markup template dung "&" tho
    # (khong phai bieu thuc Jinja) giua cac query param nen KHONG bi autoescape thanh &amp;.
    # page_size mac dinh (khong truyen tren URL) = PAGE_SIZE = 10, link phai giu nguyen gia
    # tri nay de doi trang khong lam thay doi so dong/trang dang chon.
    assert 'href="/tasks?page=2&q=&status=&page_size=10"' in resp.text


def test_middle_page_has_both_prev_and_next_links(client_factory, db_session):
    """TC2: trang giua (page=2/3) phai co ca link Prev (page=1) va Next (page=3)."""
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    make_many_tasks(db_session, 25)

    client = client_factory(user=admin)
    resp = client.get("/tasks?page=2")

    assert resp.status_code == 200
    assert 'id="pagination"' in resp.text
    assert 'href="/tasks?page=1&q=&status=&page_size=10"' in resp.text
    assert 'href="/tasks?page=3&q=&status=&page_size=10"' in resp.text


def test_last_page_has_next_disabled(client_factory, db_session):
    """TC3: trang cuoi (page=3/3) nut Next phai disable, khong co link page=4."""
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    make_many_tasks(db_session, 25)

    client = client_factory(user=admin)
    resp = client.get("/tasks?page=3")

    assert resp.status_code == 200
    assert 'aria-label="Trang sau" aria-disabled="true"' in resp.text
    assert 'href="/tasks?page=4' not in resp.text


def test_pagination_links_carry_q_and_status_and_urlencode_special_chars(client_factory, db_session):
    """TC4: link phan trang phai giu nguyen q/status qua urlencode, ke ca ky tu dac biet/
    tieng Viet + dau '&' trong q, khong lam vo URL (khong duoc chen truc tiep chuoi tho
    chua '&'/khoang trang vao href - se pha cau truc query string cua chinh no)."""
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    make_many_tasks(db_session, 25, updatefor="Sửa lỗi & kiểm tra")

    client = client_factory(user=admin)
    # Dung params=dict (khong noi chuoi tho) de chinh request cung duoc encode dung -
    # tranh "&" trong q bi httpx/TestClient hieu nham la dau phan cach query param.
    resp = client.get("/tasks", params={"page": 1, "q": "Sửa lỗi & kiểm tra", "status": "Task"})

    assert resp.status_code == 200
    # urlencode filter phai bien khoang trang/tieng Viet/dau & thanh %XX (vd %26 cho '&'),
    # dam bao href van la 1 URL hop le, khong bi cat cut o giua do dau '&' tho.
    assert (
        'href="/tasks?page=2&q=S%E1%BB%ADa%20l%E1%BB%97i%20%26%20ki%E1%BB%83m%20tra&status=Task&page_size=10"'
        in resp.text
    )


def test_page_size_param_changes_rows_shown_and_total_pages(client_factory, db_session):
    """TC: chon page_size=20 phai tra 20 dong/trang (thay vi mac dinh 10) va total_pages
    giam tuong ung (25 task -> 2 trang thay vi 3), select hien dung gia tri dang chon."""
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    make_many_tasks(db_session, 25)

    client = client_factory(user=admin)
    resp = client.get("/tasks?page=1&page_size=20")

    assert resp.status_code == 200
    assert 'option value="20" selected' in resp.text
    # Trang 1 voi page_size=20 -> con trang 2 (25 task, page_size=20 -> 2 trang), khong con
    # trang 3 nhu mac dinh page_size=10.
    assert 'href="/tasks?page=2&q=&status=&page_size=20"' in resp.text
    assert 'href="/tasks?page=3' not in resp.text


def test_page_size_out_of_bounds_returns_422(client_factory, db_session):
    """TC (edge, dam bao khong tao offset/limit vo ly): page_size <= 0 hoac > 100 phai bi
    FastAPI chan o tang validation (422), khong lot xuong toi query DB."""
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)

    client = client_factory(user=admin)
    assert client.get("/tasks?page_size=0").status_code == 422
    assert client.get("/tasks?page_size=101").status_code == 422


def test_total_pages_le_7_shows_all_pages_no_ellipsis(client_factory, db_session):
    """TC5: total_pages <= 7 (o day: 7 task, PAGE_SIZE=10 -> 1 trang) khong co dau '...'."""
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    make_many_tasks(db_session, 7)

    client = client_factory(user=admin)
    resp = client.get("/tasks?page=1")

    assert resp.status_code == 200
    # Chi kiem tra BEN TRONG khoi #pagination - class CSS ".page-ellipsis" luon co san trong
    # <style> o base.html du khong dung toi, nen khong the assert tren toan bo resp.text.
    pagination_html = resp.text.split('id="pagination"', 1)[1].split("</div>", 1)[0]
    assert "page-ellipsis" not in pagination_html
    assert ">…<" not in pagination_html


def test_build_page_items_small_total_shows_all_no_none():
    """TC6: total_pages <= 7 -> danh sach day du, khong co None (dau '...')."""
    assert _build_page_items(1, 7) == [1, 2, 3, 4, 5, 6, 7]
    assert _build_page_items(7, 7) == [1, 2, 3, 4, 5, 6, 7]
    assert None not in _build_page_items(1, 1)


def test_build_page_items_middle_page_has_ellipsis_both_sides():
    """TC7: page=7, total=20 (o giua xa 2 dau) -> co None (…) o ca 2 phia:
    1 ... 6 7 8 ... 20."""
    assert _build_page_items(7, 20) == [1, None, 6, 7, 8, None, 20]


def test_build_page_items_boundaries_page_1_and_page_total():
    """TC8: page=1 va page=total_pages (bien) voi total lon van tra danh sach hop le,
    luon co trang 1 va trang cuoi, khong loi index/None sai vi tri."""
    items_first = _build_page_items(1, 20)
    items_last = _build_page_items(20, 20)

    assert items_first[0] == 1
    assert items_first[-1] == 20
    assert items_last[0] == 1
    assert items_last[-1] == 20


def test_build_page_items_out_of_range_page_does_not_crash():
    """TC9 (edge): page ngoai pham vi hop le (0, am, > total_pages) khong duoc raise loi -
    _build_page_items chi tinh 'o' hien thi, khong tu validate page co that hay khong (route
    da tu clamp offset o tang query)."""
    assert _build_page_items(0, 20) == _build_page_items(1, 20)
    assert _build_page_items(-5, 20) == _build_page_items(1, 20)
    assert _build_page_items(999, 20) == _build_page_items(20, 20)
