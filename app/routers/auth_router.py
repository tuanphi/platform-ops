import threading
import time
from collections import OrderedDict
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import check_csrf_token, get_current_user_optional, get_or_create_csrf_token, get_or_create_user, oauth
from app.config import get_settings
from app.database import get_db
from app.models import User
from app.services.password_service import hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])
templates = Jinja2Templates(directory="app/templates")
settings = get_settings()

# Hash "gia" tinh 1 lan luc import - dung lam target cho verify_password() khi email khong
# ton tai/khong co password_hash, de thoi gian xu ly LUON gan bang nhau du email co that
# hay khong (PBKDF2 260k iterations la phan ton thoi gian chinh) - chan timing side-channel
# do gia login sai ngay lap tuc (khong chay verify_password) khi email khong ton tai.
_DUMMY_PASSWORD_HASH = hash_password("dummy-password-not-a-real-account")

# Rate limit dang nhap password theo email (dict in-process + Lock, KHONG can Redis/DB
# rieng vi app chi chay 1 process uvicorn) - chan brute-force/credential-stuffing vao 1
# tai khoan cu the. KHAC voi _check_run_rollback_rate_limit trong actions_router.py (key
# la email cua user DA DANG NHAP, bi chan boi so nhan vien noi bo huu han): key o day la
# email TRONG FORM, do NGUOI CHUA XAC THUC tu do cung cap - attacker co the flood hang loat
# email KHAC NHAU de lam dict phinh to. Dung OrderedDict lam LRU voi TRAN CUNG
# (_LOGIN_ATTEMPT_STATE_MAX_ENTRIES): moi lan ghi nhan that bai, day entry vua cap nhat ra
# cuoi (move_to_end) roi loai entry CU NHAT (popitem(last=False), O(1)) neu vuot tran -
# dam bao bo nho bi chan cung, KHONG can quet toan bo dict duoi lock (cach cu dua vao
# "stale > 300s" khong dam bao tran neu attacker lien tuc dung email moi trong cung 1 cua
# so 300s, va viec quet duoi lock tren moi request se tu bien no thanh 1 vector DoS khac).
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_LOCKOUT_SECONDS = 300  # 5 phut
_LOGIN_ATTEMPT_STATE_MAX_ENTRIES = 2000

_login_attempts_lock = threading.Lock()
_login_attempts: "OrderedDict[str, tuple[int, float]]" = OrderedDict()  # email(lower) -> (so lan sai lien tiep, thoi diem sai gan nhat)


def _check_login_rate_limit(email_key: str) -> bool:
    """Tra False neu email nay da sai >= _LOGIN_MAX_ATTEMPTS lan trong _LOGIN_LOCKOUT_SECONDS
    giay gan day - khong duoc thu dang nhap tiep (ke ca dung mat khau dung, vi luc nay
    khong the phan biet duoc voi request cua ke tan cong dang do)."""
    now = time.monotonic()
    with _login_attempts_lock:
        entry = _login_attempts.get(email_key)
        if entry is None:
            return True
        count, last_ts = entry
        if now - last_ts > _LOGIN_LOCKOUT_SECONDS:
            return True  # qua khoi cua so tinh, coi nhu da het khoa
        return count < _LOGIN_MAX_ATTEMPTS


def _record_login_failure(email_key: str) -> None:
    now = time.monotonic()
    with _login_attempts_lock:
        entry = _login_attempts.get(email_key)
        count = entry[0] if entry is not None and now - entry[1] <= _LOGIN_LOCKOUT_SECONDS else 0
        _login_attempts[email_key] = (count + 1, now)
        _login_attempts.move_to_end(email_key)
        while len(_login_attempts) > _LOGIN_ATTEMPT_STATE_MAX_ENTRIES:
            _login_attempts.popitem(last=False)


def _clear_login_failures(email_key: str) -> None:
    with _login_attempts_lock:
        _login_attempts.pop(email_key, None)


def _stamp_login(request: Request) -> None:
    """Danh dau thoi diem dang nhap (epoch giay, UTC) vao session - lam moc tinh auto-logout
    TUYET DOI theo tung thiet bi/session sau N ngay, doc lap voi hoat dong cua user (xem
    app/auth.py::_session_expired). Phai goi NGAY SAU khi set session["user_id"] o CA 3 diem
    dang nhap (Google OAuth callback, password login, dev-login) de khong sot luong nao."""
    request.session["login_at"] = int(datetime.utcnow().timestamp())


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, user: User | None = Depends(get_current_user_optional)):
    if user:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "google_enabled": bool(settings.google_client_id),
            "dev_mode": settings.auth_dev_mode,
            "password_enabled": settings.auth_enable_password,
            "error": request.session.pop("login_error", None),
            "csrf_token": get_or_create_csrf_token(request),
        },
    )


@router.get("/google")
async def login_google(request: Request):
    redirect_uri = settings.google_callback_url
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/google/callback")
async def auth_callback(request: Request, db: Session = Depends(get_db)):
    token = await oauth.google.authorize_access_token(request)
    userinfo = token.get("userinfo") or {}
    email = userinfo.get("email")
    name = userinfo.get("name", "")
    if not email:
        return RedirectResponse("/auth/login", status_code=303)

    user = get_or_create_user(db, email=email, name=name)
    request.session["user_id"] = user.id
    _stamp_login(request)
    return RedirectResponse("/", status_code=303)


@router.post("/login")
def password_login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
    remember: str | None = Form(None),
    db: Session = Depends(get_db),
):
    if not settings.auth_enable_password:
        return RedirectResponse("/auth/login", status_code=303)
    if not check_csrf_token(request, csrf_token):
        request.session["login_error"] = "Phiên đăng nhập đã hết hạn, vui lòng thử lại"
        return RedirectResponse("/auth/login", status_code=303)

    email_key = email.strip().lower()
    if not _check_login_rate_limit(email_key):
        request.session["login_error"] = "Tài khoản tạm thời bị khóa do đăng nhập sai quá nhiều lần, vui lòng thử lại sau ít phút"
        return RedirectResponse("/auth/login", status_code=303)

    user = db.query(User).filter(User.email == email).first()
    # Luon chay verify_password (co that hay dummy hash) bat ke user co ton tai hay khong -
    # chan timing side-channel de lo user nao ton tai trong he thong qua thoi gian phan hoi
    # (nhanh neu khong ton tai/bo qua PBKDF2, cham neu ton tai/chay du PBKDF2).
    password_hash = user.password_hash if (user and user.is_active and user.password_hash) else _DUMMY_PASSWORD_HASH
    password_ok = verify_password(password, password_hash)
    if user is None or not user.is_active or not user.password_hash or not password_ok:
        _record_login_failure(email_key)
        request.session["login_error"] = "Email hoặc mật khẩu không đúng"
        return RedirectResponse("/auth/login", status_code=303)

    _clear_login_failures(email_key)
    request.session["user_id"] = user.id
    _stamp_login(request)
    request.session["remember"] = remember is not None
    return RedirectResponse("/", status_code=303)


@router.post("/dev-login")
def dev_login(request: Request, email: str = Form(...), csrf_token: str = Form(...), db: Session = Depends(get_db)):
    if not settings.auth_dev_mode:
        return RedirectResponse("/auth/login", status_code=303)
    if not check_csrf_token(request, csrf_token):
        request.session["login_error"] = "Phiên đăng nhập đã hết hạn, vui lòng thử lại"
        return RedirectResponse("/auth/login", status_code=303)
    user = get_or_create_user(db, email=email, name=email.split("@")[0])
    request.session["user_id"] = user.id
    _stamp_login(request)
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/auth/login", status_code=303)
