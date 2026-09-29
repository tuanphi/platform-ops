import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.auth import RedirectToLogin, RememberMeMiddleware
from app.config import get_settings
from app.database import SessionLocal
from app.routers import (
    actions_router,
    auth_router,
    catalog_router,
    git_webhook_router,
    production_router,
    settings_router,
    staging_router,
    sync_events_router,
    tasks_router,
    telegram_router,
    users_router,
)
from app.scheduler import shutdown_scheduler, start_scheduler
from app.services import task_service
from app.services.notify_service import send_telegram, task_summary_lines

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)
settings = get_settings()


def _recover_stuck_running_tasks_on_startup() -> None:
    """Chay 1 LAN luc app khoi dong, BAT BUOC goi TRUOC start_scheduler() (job
    process_task_queue chua duoc dang ky luc nay nen khong the co xung dot voi chinh
    worker cua tien trinh hien tai) - xem task_service.recover_stuck_running_tasks de biet
    day du gia dinh (dac biet rui ro neu sau nay deploy nhieu replica) va ly do chon trang
    thai phuc hoi (KHONG tu dong chay lai git). Loi ngoai du kien (vd DB tam thoi khong
    ket noi duoc) chi log, KHONG duoc phep chan app khoi dong."""
    db = SessionLocal()
    try:
        recovered = task_service.recover_stuck_running_tasks(db)
    except Exception:
        logger.exception("Phuc hoi task ket o Running luc app khoi dong that bai ngoai du kien")
        return
    finally:
        db.close()

    if not recovered:
        return

    logger.warning(
        "Da phuc hoi %s task ket o Running luc app khoi dong (nghi ngo crash/restart truoc do)",
        len(recovered),
    )
    try:
        for task in recovered:
            send_telegram(
                "🛠️ <b>The task is restored after the app restarts</b>\n"
                + task_summary_lines(task)
                + f"\n<b>New status:</b> {task.status.value}"
                + "\n<b>Reason:</b> Suspected app crash/restart during git execution."
                " - please manually check ArgoCD/git history before re-running"
            )
    except Exception:
        logger.exception("Gui Telegram thong bao phuc hoi task that bai (khong anh huong task da duoc phuc hoi)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Schema DB do Alembic quan ly rieng (chay `alembic upgrade head` truoc khi start),
    # app khong tu tao/sua bang khi khoi dong.
    _recover_stuck_running_tasks_on_startup()
    start_scheduler()
    yield
    shutdown_scheduler()


# Chi expose /docs, /redoc, /openapi.json khi chay local (APP_URL tro localhost/...) -
# production khong duoc cong khai API schema/UI thu nghiem nay cho nguoi chua xac thuc.
_docs_enabled = settings.is_local_app_url()
app = FastAPI(
    title="form-deploy",
    lifespan=lifespan,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    # Dung SESSION_REMEMBER_MAX_HOURS (tran cung, doc tu .env, mac dinh 1 tuan) thay vi
    # session_remember_hours (gia tri co the sua qua UI/DB) de dam bao server KHONG BAO
    # GIO chap nhan chu ky cookie song lau hon MAX du gia tri dang luu trong DB la bao
    # nhieu - RememberMeMiddleware van thu hep Max-Age cookie o trinh duyet theo gia tri
    # DB cho tung request, MAX o day chi la tran an toan phia server.
    max_age=settings.session_remember_max_hours * 3600,
    # Chi bat Secure cookie khi APP_URL that su la https:// - production luon phai la
    # https nen cookie session khong duoc gui qua HTTP thuong (chan sniffing/MITM tren
    # mang khong tin cay); dev local qua http://localhost van chay binh thuong vi cookie
    # Secure se bi trinh duyet/TestClient bo qua tren HTTP.
    https_only=settings.app_url.startswith("https://"),
)
app.add_middleware(RememberMeMiddleware)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Chan clickjacking (X-Frame-Options) va MIME-sniffing (X-Content-Type-Options) tren
    moi response - KHONG dat Content-Security-Policy vi cac template dung inline
    <script>/style= rong rai (xem base.html), 1 CSP chat se lam vo UI hien tai."""
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@app.exception_handler(RedirectToLogin)
def redirect_to_login_handler(request: Request, exc: RedirectToLogin):
    return RedirectResponse("/auth/login", status_code=303)


app.include_router(auth_router.router)
app.include_router(tasks_router.router)
app.include_router(actions_router.router)
app.include_router(catalog_router.router)
app.include_router(settings_router.router)
app.include_router(users_router.router)
app.include_router(telegram_router.router)
app.include_router(git_webhook_router.router)
app.include_router(sync_events_router.router)
app.include_router(staging_router.router)
app.include_router(production_router.router)
