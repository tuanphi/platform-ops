"""Pytest fixtures dùng chung.

QUAN TRỌNG: mọi fixture ở đây dùng SQLite in-memory riêng biệt, KHÔNG bao giờ
đụng tới file DB thật (`form_deploy.db`) hay gọi git/Telegram/SMTP/ArgoCD thật -
các service ngoài đều được monkeypatch/mocked.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Đảm bảo Settings() không vô tình đọc phải .env thật của repo (đặc biệt
# DATABASE_URL) trước khi các fixture kịp override - ép in-memory sqlite ngay
# từ lúc import app.config lần đầu.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("AUTH_DEV_MODE", "true")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Role, User


@pytest.fixture()
def db_session():
    """1 engine SQLite in-memory MỚI cho mỗi test - cô lập hoàn toàn, không lấy
    engine thật của app.database (vốn trỏ vào DATABASE_URL của .env).

    StaticPool bắt buộc phải dùng ở đây: route sync (def, không phải async def) được
    Starlette chạy trong 1 thread pool riêng (run_in_threadpool) - nếu không ép dùng
    1 connection duy nhất, SQLite ':memory:' sẽ tạo 1 DB rỗng MỚI cho mỗi thread khác
    nhau (mất hết bảng vừa create_all), gây lỗi 'no such table' khi test qua TestClient.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


_CSRF_TOKEN_HTML_RE = re.compile(r'name="csrf_token" value="([^"]*)"')


def extract_csrf_token(html: str) -> str:
    """Bóc token CSRF THẬT (do server render qua Jinja global `csrf_token(request)`, xem
    app/auth.py::get_or_create_csrf_token) từ 1 trang HTML - dùng cho test cần token HỢP LỆ
    thay vì tự bịa 1 chuỗi giả (server chỉ chấp nhận token khớp với session hiện tại,
    check_csrf_token dùng secrets.compare_digest)."""
    match = _CSRF_TOKEN_HTML_RE.search(html)
    assert match, "Không tìm thấy input csrf_token trong HTML - trang phải luôn render ít nhất 1 form"
    return match.group(1)


def _wrap_client_auto_csrf(client):
    """Tự động nhét CSRF token HỢP LỆ (lấy 1 lần qua GET /tasks thật - trang này luôn có
    sẵn form bulk ẩn cho MỌI role đã đăng nhập, xem task_list.html - rồi cache lại cho cả
    đời client) vào MỌI POST dạng form (`data` là dict) CHƯA có sẵn key "csrf_token" - để
    ~50 test viết TRƯỚC KHI có CSRF (permission/rate-limit/dmy-parsing/flash-redirect/queue
    worker...) không phải sửa tay từng chỗ, nhưng vẫn đi qua ĐÚNG cơ chế CSRF thật (KHÔNG
    monkeypatch/bypass check_csrf_token).

    Chỉ fetch token 1 LẦN DUY NHẤT (cache), KHÔNG fetch lại trước mỗi POST: vì GET /tasks
    (list_tasks) có pop_flash() - nếu gọi lại giữa 2 POST trong cùng 1 test, sẽ vô tình
    "nuốt mất" flash message mà POST trước đó vừa set, gây false-negative cho các test kiểm
    tra flash/redirect. Gọi 1 lần lúc session còn trống (trước bất kỳ action nào) thì
    pop_flash không có gì để nuốt - an toàn tuyệt đối.

    Test RIÊNG VỀ CSRF (thiếu/sai/token của session khác) phải tự kiểm soát: hoặc tự điền
    sẵn key "csrf_token" trong `data` (wrapper sẽ KHÔNG ghi đè giá trị đã có, kể cả rỗng/sai),
    hoặc gọi thẳng `client.post_without_csrf(...)` (tham chiếu hàm post GỐC, hoàn toàn không
    qua wrapper này) để mô phỏng request KHÔNG gửi trường csrf_token nào cả."""
    original_post = client.post
    cache: dict[str, str] = {}

    def _post(url, *args, **kwargs):
        data = kwargs.get("data")
        if isinstance(data, dict) and "csrf_token" not in data:
            if "token" not in cache:
                cache["token"] = extract_csrf_token(client.get("/tasks").text)
            data = dict(data)
            data["csrf_token"] = cache["token"]
            kwargs["data"] = data
        return original_post(url, *args, **kwargs)

    client.post = _post
    client.post_without_csrf = original_post
    return client


def make_user(db_session, email="user@example.com", role=Role.USER, **kwargs) -> User:
    user = User(email=email, role=role, **kwargs)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def make_user_factory(db_session):
    def _factory(email="user@example.com", role=Role.USER, **kwargs):
        return make_user(db_session, email=email, role=role, **kwargs)

    return _factory


@pytest.fixture()
def client_factory(db_session, monkeypatch):
    """TestClient(app) với get_db + get_current_user override sang db_session/test-user
    truyền vào, scheduler và notify_service (Telegram/SMTP) bị vô hiệu hoá hoàn toàn -
    KHÔNG bao giờ chạm engine thật của app.database (vốn trỏ .env DATABASE_URL) hay gọi
    mạng thật ra ngoài."""
    from fastapi.testclient import TestClient

    import app.auth as auth_module
    import app.database as database_module
    import app.main as main_module
    import app.routers.actions_router as actions_router_module
    from app.services import notify_service, restart_service

    # Vo hieu hoa scheduler that (khong khoi dong APScheduler / khong mo session
    # rieng toi app.database.engine that trong luc startup/shutdown).
    monkeypatch.setattr(main_module, "start_scheduler", lambda: None)
    monkeypatch.setattr(main_module, "shutdown_scheduler", lambda: None)

    # Reset rate-limit Run/Rollback (dict in-process trong actions_router.py) ve rong
    # truoc MOI test - dict nay la state cap MODULE (dung chung xuyen suot tien trinh
    # pytest, khong tu dong reset giua cac test) nen neu khong reset, 1 test goi Run/
    # Rollback voi 1 email vua duoc dung o test truoc do (trong cua so cooldown vai giay)
    # co the bi chan oan, gay flaky/false-negative KHONG lien quan gi toi logic dang test.
    monkeypatch.setattr(actions_router_module, "_run_rollback_last_call", {})

    # Reset rate-limit Restart (dict in-process trong restart_service.py, cung pattern
    # voi _run_rollback_last_call o tren) ve rong truoc MOI test - vi cung la state cap
    # MODULE khoa theo (env_label, project, application) nen 2 test doc lap vo tinh dung
    # trung 1 bo khoa nay trong cua so cooldown 30s se lam test chay sau bi chan oan neu
    # khong reset o day.
    monkeypatch.setattr(restart_service, "_restart_last_call", {})

    # Reset rate-limit Restart theo user.id (vong vá 2, CWE-770 - dict MOI trong
    # restart_service.py, khac dict o tren) ve rong truoc MOI test - cung la state cap
    # MODULE (khoa theo user.id, cua so 60s) nen 2 test doc lap vo tinh dung chung 1 user.id
    # (vd cung dung make_user mac dinh email="user@example.com" -> cung id trong DB test) se
    # lam test chay sau bi 429 oan neu khong reset o day.
    monkeypatch.setattr(restart_service, "_restart_attempt_state", {})

    # Vo hieu hoa moi thong bao ra ngoai (Telegram/SMTP that) - da co test rieng cho
    # notify_service, o day chi test logic router/permission.
    monkeypatch.setattr(notify_service, "send_telegram", lambda *a, **k: None)
    monkeypatch.setattr(notify_service, "send_mail", lambda *a, **k: None)

    def _override_get_db():
        yield db_session

    def _make_client(user=None):
        main_module.app.dependency_overrides[database_module.get_db] = _override_get_db
        if user is not None:
            main_module.app.dependency_overrides[auth_module.get_current_user] = lambda: user
        else:
            main_module.app.dependency_overrides.pop(auth_module.get_current_user, None)
        client = TestClient(main_module.app)
        if user is not None:
            # Chi wrap khi da dang nhap - client user=None dung de test luong chua dang
            # nhap (redirect ve /auth/login), khong bao gio cham toi cac route can CSRF.
            _wrap_client_auto_csrf(client)
        return client

    yield _make_client

    main_module.app.dependency_overrides.clear()
