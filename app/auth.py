import logging
import re
import secrets
from datetime import datetime

from authlib.integrations.starlette_client import OAuth
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings
from app.database import get_db
from app.models import Role, User
from app.services.app_setting_service import get_app_setting

logger = logging.getLogger(__name__)
settings = get_settings()

oauth = OAuth()
if settings.google_client_id and settings.google_client_secret:
    oauth.register(
        name="google",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


class RedirectToLogin(Exception):
    """Raised bởi dependency khi chưa đăng nhập; main.py có exception handler chuyển thành redirect."""


def get_or_create_csrf_token(request: Request) -> str:
    """Tra token CSRF luu trong session, tao moi (secrets.token_urlsafe) neu chua co.

    Dat o app/auth.py (module dung chung, khong router nao phai import cheo router khac)
    vi ham nay duoc dung boi CA auth_router.py (chan Login CSRF cho form login/dev-login)
    LAN tasks_router.py/actions_router.py (chan CSRF cho cac form doi trang thai deploy:
    Approve/Reject/Run/Cancel/Rollback/Auto-deploy/Bulk...). Ban dau ham nay chi ton tai
    rieng trong auth_router.py, chuyen sang day khi mo rong pham vi ap dung CSRF."""
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def check_csrf_token(request: Request, csrf_token: str) -> bool:
    """So sanh token client gui len voi token dang luu trong session bang
    secrets.compare_digest (chan timing attack do bao thu token). Tra False neu session
    chua co token nao (vd chua bao gio GET trang co form) hoac token khong khop."""
    expected = request.session.get("csrf_token")
    return bool(expected) and secrets.compare_digest(csrf_token, expected)


def get_or_create_user(db: Session, email: str, name: str = "") -> User:
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        user = User(email=email, name=name, role=Role.USER)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def _session_expired(request: Request, db: Session) -> bool:
    """Auto-logout tuyet doi theo tung thiet bi: moi session duoc dong dau "login_at"
    (epoch giay, xem _stamp_login trong auth_router.py) luc dang nhap - het han neu qua
    N gio ke tu do, BAT KE user co hoat dong lien tuc hay khong (khac voi RememberMeMiddleware
    chi dieu chinh Max-Age cookie phia trinh duyet). N doc DONG (moi request) tu
    AppSetting.session_remember_hours de Super Admin doi qua UI co hieu luc ngay, fallback ve
    .env neu DB loi, roi luon KEP vao [session_remember_min_hours, session_remember_max_hours]
    (.env, tran cung) - dam bao gia tri du lay tu nguon nao cung khong the vuot tran an toan
    cua SessionMiddleware (max_age=session_remember_max_hours trong app/main.py)."""
    login_at = request.session.get("login_at")
    if not isinstance(login_at, (int, float)):
        return True
    try:
        n_hours = get_app_setting(db).session_remember_hours
    except Exception:
        logger.exception("Khong doc duoc session_remember_hours tu AppSetting, fallback ve .env")
        n_hours = get_settings().session_remember_hours
    env = get_settings()
    n_hours = max(env.session_remember_min_hours, min(n_hours, env.session_remember_max_hours))
    now = int(datetime.utcnow().timestamp())
    return (now - int(login_at)) > n_hours * 3600


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise RedirectToLogin()
    if _session_expired(request, db):
        request.session.clear()
        raise RedirectToLogin()
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise RedirectToLogin()
    return user


def get_current_user_optional(request: Request, db: Session = Depends(get_db)) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    if _session_expired(request, db):
        request.session.clear()
        return None
    return db.get(User, user_id)


def require_settings_access(user: User = Depends(get_current_user)) -> User:
    if not user.can_manage_settings:
        raise HTTPException(status_code=403, detail="Permission Denied")
    return user


_COOKIE_EXPIRY_RE = re.compile(r";\s*(?:expires|Max-Age)=[^;]*", re.IGNORECASE)


class RememberMeMiddleware(BaseHTTPMiddleware):
    """Chinh lai Max-Age/expires cua cookie session ma SessionMiddleware vua set, cho
    tung request cua user da dang nhap:
    - Neu route login khong tick 'Ghi nho dang nhap' (session['remember'] = False): bo
      het Max-Age/expires - bien no thanh session cookie (mat khi dong trinh duyet).
    - Nguoc lai (remember True/mac dinh): ghi de bang gia tri song hien tai doc TRUC
      TIEP tu DB (AppSetting.session_remember_hours) o MOI request, thay vi dung
      Max-Age co dinh ma SessionMiddleware tinh 1 lan tu Settings.session_remember_hours
      (.env) luc app khoi dong. SessionMiddleware gui lai Set-Cookie tren moi response
      co session (xem starlette.middleware.sessions.SessionMiddleware.send_wrapper) nen
      cach nay giup Super Admin doi gia tri qua UI /settings/general co hieu luc ngay
      tu request ke tiep - ke ca cho session dang dang nhap san - khong can restart app.

    Phai duoc add_middleware SAU SessionMiddleware trong main.py de nam ngoai no
    (chay sau cung tren response, doc duoc Set-Cookie ma SessionMiddleware vua ghi)."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        session = request.scope.get("session")
        if session is not None and session.get("user_id"):
            cookies = response.headers.getlist("set-cookie")
            if cookies:
                del response.headers["set-cookie"]
                for cookie in cookies:
                    if cookie.startswith("session="):
                        cookie = _COOKIE_EXPIRY_RE.sub("", cookie)
                        if session.get("remember", True):
                            try:
                                remember_hours = get_app_setting().session_remember_hours
                            except Exception:
                                # DB tam thoi khong doc duoc (vd loi ket noi) - fallback
                                # ve gia tri .env thay vi lam sap request cua user dang
                                # dang nhap chi vi khong ghi de duoc Max-Age.
                                logger.exception(
                                    "Khong doc duoc session_remember_hours tu AppSetting, "
                                    "fallback ve Settings.session_remember_hours (.env)"
                                )
                                remember_hours = get_settings().session_remember_hours
                            max_age = remember_hours * 3600
                            cookie = f"{cookie}; Max-Age={max_age}"
                    response.headers.append("set-cookie", cookie)
        return response
