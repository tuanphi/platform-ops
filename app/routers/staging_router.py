"""Bật/Tắt staging: sửa field `replicas` trong file values-staging.yaml (đường dẫn cấu
hình ở Settings > General) trên nhánh staging, thông báo qua Telegram bot/chat RIÊNG,
độc lập hoàn toàn với luồng deploy (Task/Approve/Run). Menu riêng, xem
app/services/staging_service.py cho business logic."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import check_csrf_token, get_current_user, get_or_create_csrf_token
from app.database import get_db
from app.flash import is_ajax, pop_flash, set_flash
from app.models import User
from app.services import restart_service, staging_service
from app.services.git_service import GitError

router = APIRouter(prefix="/staging", tags=["staging"])
templates = Jinja2Templates(directory="app/templates")
# Global function de template goi truc tiep {{ csrf_token(request) }} - moi Jinja2Templates()
# la 1 Environment rieng nen phai dang ky lai o day (giong actions_router.py/tasks_router.py),
# dung cho form Restart moi (mutate ArgoCD, xem POST /staging/restart ben duoi).
templates.env.globals["csrf_token"] = get_or_create_csrf_token


def _redirect(request: Request, ok: bool, message: str, status_code: int = 200) -> Response:
    if is_ajax(request):
        return JSONResponse({"ok": ok, "message": message}, status_code=status_code)
    set_flash(request, ok, message)
    return RedirectResponse("/staging", status_code=303)


@router.get("", response_class=HTMLResponse)
def staging_page(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not user.can_access_staging:
        raise HTTPException(status_code=403, detail="Permission Denied")
    apps = staging_service.list_apps_with_replicas(db)
    total_apps = len(apps)
    running_apps = sum(1 for a in apps if a["enabled"])
    running_pods = sum(
        int(a["replicas"]) for a in apps if a["enabled"] and str(a["replicas"]).isdigit()
    )
    flash_ok, flash_msg = pop_flash(request)
    return templates.TemplateResponse(
        "staging.html",
        {
            "request": request,
            "user": user,
            "apps": apps,
            "total_apps": total_apps,
            "running_apps": running_apps,
            "running_pods": running_pods,
            "flash_ok": flash_ok,
            "flash_msg": flash_msg,
        },
    )


@router.post("/sync")
def sync(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Nut "Lam moi" - dong bo lai cache tu git ngay lap tuc (giong nut "Sync catalog"),
    thay vi cho scheduler chay dinh ky. Day la hanh dong GHI (dong bo du chi doc tu git,
    van MUTATE cache DB cua app) - dung can_write_staging (KHONG dung can_access_staging,
    property do gio bao gom ca user chi co quyen doc)."""
    if not user.can_write_staging:
        return _redirect(request, False, "Bạn không có quyền dùng tính năng Thay đổi pod Staging")
    try:
        summary = staging_service.sync_apps(db)
    except GitError as exc:
        return _redirect(request, False, f"Đồng bộ thất bại: {exc}")
    return _redirect(request, True, f"Đã đồng bộ {summary['total']} ứng dụng từ git")


@router.post("/toggle")
def toggle(
    request: Request,
    project: str = Form(...),
    application: str = Form(...),
    enabled: str = Form(...),
    replicas: int | None = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user.can_write_staging:
        return _redirect(request, False, "Bạn không có quyền dùng tính năng Thay đổi pod Staging")
    result = staging_service.toggle_app(db, user, project, application, enabled == "1", replicas)
    return _redirect(request, result.ok, result.message)


@router.post("/bulk-toggle")
def bulk_toggle(
    request: Request,
    apps: list[str] = Form(...),
    enabled: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user.can_write_staging:
        return _redirect(request, False, "Bạn không có quyền dùng tính năng Thay đổi pod Staging")
    parsed: list[tuple[str, str]] = []
    for item in apps:
        # Moi item dang "project|application" (xem staging.html - value cua checkbox
        # .staging-select), bo qua item sai dinh dang thay vi 400 ca request.
        if "|" not in item:
            continue
        project, application = item.split("|", 1)
        parsed.append((project, application))
    result = staging_service.bulk_toggle(db, user, parsed, enabled == "1")
    return _redirect(request, result.ok, result.message)


@router.post("/restart")
def restart(
    request: Request,
    project: str = Form(...),
    application: str = Form(...),
    csrf_token: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Nut "Restart" 1 dong app - khoi dong lai workload (Deployment/StatefulSet) qua
    ArgoCD API (xem app/services/argocd_service.py::restart_workload), KHONG dung
    kubectl/kubeconfig, KHONG doi replicas/cot "Trang thai" hien co. Mutate ArgoCD nen bat
    buoc CSRF (giong pattern actions_router.py), KHAC voi /toggle hien co CHUA co CSRF.

    Rate-limit theo user.id (restart_service.check_restart_attempt_rate_limit) PHAI la buoc
    DAU TIEN, TRUOC CA check_csrf_token() - vong vá 2 ([sec]): neu dat SAU CSRF-check thi 1
    user da dang nhap van co the spam CSRF token sai vo han, moi lan deu lot qua roi bi
    record_blocked_attempt() ghi 1 dong RestartLog + 1 dong log warning KHONG qua bat ky
    rate-limit nao (CWE-770, phinh DB/log vo han). Khi bi chan o day: tra 429, KHONG goi
    record_blocked_attempt (khong ghi RestartLog) - dung muc dich cua ban vá la chan chinh
    vong lap ghi-log-vo-han nay, khong phai tao them 1 nguon ghi log khac.

    Quyen rieng can_restart_staging/can_restart_production (tu 2026-08-18, TACH BIET voi
    can_write_staging/can_write_production - quyen sua replicas) - 1 user co the duoc
    cap Restart ma khong duoc sua replicas, hoac nguoc lai."""
    if not restart_service.check_restart_attempt_rate_limit(user.id):
        return _redirect(
            request, False, restart_service.RESTART_ATTEMPT_RATE_LIMIT_MESSAGE, status_code=429
        )
    if not check_csrf_token(request, csrf_token):
        restart_service.record_blocked_attempt(
            db, user, staging_service.ENV_LABEL, project, application, "CSRF token không hợp lệ hoặc hết hạn"
        )
        return _redirect(request, False, "Phiên làm việc đã hết hạn, vui lòng tải lại trang và thử lại")
    if not user.can_restart_staging:
        restart_service.record_blocked_attempt(
            db, user, staging_service.ENV_LABEL, project, application, "Không có quyền dùng tính năng Restart Staging"
        )
        return _redirect(request, False, "Bạn không có quyền dùng tính năng Restart Staging")
    result = restart_service.restart_app(db, user, staging_service.ENV_LABEL, project, application)
    return _redirect(request, result.ok, result.message)


@router.post("/bulk-restart")
def bulk_restart(
    request: Request,
    apps: list[str] = Form(...),
    csrf_token: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Nut "Restart" trong toolbar bulk (khi tick nhieu checkbox) - KHAC /bulk-toggle (1
    GIT COMMIT duy nhat cho ca batch): restart_service.bulk_restart_apps() lap goi
    restart_app() (dung 1 cho, KHONG lap logic rieng) cho TUNG app mot, moi app DOC LAP
    hoan toan (1 app that bai/dinh cooldown KHONG chan cac app con lai). Cung quyen
    can_restart_staging voi nut Restart don (KHONG phai can_write_staging), cung rate-limit
    theo user.id (check_restart_attempt_rate_limit dung CHUNG dict voi nut Restart don -
    1 request bulk = 1 lan goi ham nay, KHONG tinh theo so app ben trong)."""
    if not restart_service.check_restart_attempt_rate_limit(user.id):
        return _redirect(
            request, False, restart_service.RESTART_ATTEMPT_RATE_LIMIT_MESSAGE, status_code=429
        )
    if not check_csrf_token(request, csrf_token):
        return _redirect(request, False, "Phiên làm việc đã hết hạn, vui lòng tải lại trang và thử lại")
    if not user.can_restart_staging:
        return _redirect(request, False, "Bạn không có quyền dùng tính năng Restart Staging")
    parsed: list[tuple[str, str]] = []
    for item in apps:
        if "|" not in item:
            continue
        project, application = item.split("|", 1)
        parsed.append((project, application))
    result = restart_service.bulk_restart_apps(db, user, staging_service.ENV_LABEL, parsed)
    return _redirect(request, result.ok, result.message)


@router.post("/turn-off-all")
def turn_off_all(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # can_turn_off_all_staging (role) khong tu dong bao gom can_write_staging (cong tac
    # duoc Super Admin cap) - Admin CHI duoc cap can_view_staging (chi xem) van thoa dieu
    # kien role o day nhung KHONG duoc phep sua gi, nen phai kiem CA HAI.
    if not user.can_write_staging or not user.can_turn_off_all_staging:
        return _redirect(request, False, "Bạn không có quyền Tắt tất cả staging")
    result = staging_service.turn_off_all(db, user)
    return _redirect(request, result.ok, result.message)
