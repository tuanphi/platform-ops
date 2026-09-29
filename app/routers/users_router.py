"""Quan ly rieng cho user: doi role + dat mat khau dang nhap. Chi Super Admin."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import require_settings_access
from app.database import get_db
from app.flash import is_ajax, pop_flash, set_flash
from app.models import Role, User
from app.services.password_service import hash_password

router = APIRouter(prefix="/users", tags=["users"])
templates = Jinja2Templates(directory="app/templates")


def _respond(request: Request, ok: bool, message: str) -> Response:
    if is_ajax(request):
        return JSONResponse({"ok": ok, "message": message})
    set_flash(request, ok, message)
    return RedirectResponse("/users", status_code=303)


@router.get("", response_class=HTMLResponse)
def users_page(request: Request, user: User = Depends(require_settings_access), db: Session = Depends(get_db)):
    users = db.query(User).order_by(User.email).all()
    flash_ok, flash_msg = pop_flash(request)
    return templates.TemplateResponse(
        "users.html",
        {
            "request": request,
            "user": user,
            "users": users,
            "roles": [r.value for r in Role],
            "flash_ok": flash_ok,
            "flash_msg": flash_msg,
        },
    )


@router.post("/{user_id}/role")
def set_user_role(
    request: Request,
    user_id: int,
    role: str = Form(...),
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    target_user = db.get(User, user_id)
    if target_user is None:
        return _respond(request, False, "User không tồn tại")
    try:
        target_user.role = Role(role)
    except ValueError:
        return _respond(request, False, f"Role '{role}' không hợp lệ")
    db.commit()
    message = f"Đã đổi role '{target_user.email}' -> {target_user.role.value}"
    if is_ajax(request):
        # Tra kem role/6 co quyen de client tu cap nhat lai 6 cot Staging/Production
        # Replicas/Restart/Read CUNG HANG NGAY (khong reload) - vi giao dien cac cot do
        # phu thuoc truc tiep vao role (Super Admin luon hien nut bat+khoa, khac han cac
        # role con lai).
        return JSONResponse(
            {
                "ok": True,
                "message": message,
                "role": target_user.role.value,
                "can_toggle_staging": target_user.can_toggle_staging,
                "can_toggle_production": target_user.can_toggle_production,
                "can_view_staging": target_user.can_view_staging,
                "can_view_production": target_user.can_view_production,
                "can_toggle_restart_staging": target_user.can_toggle_restart_staging,
                "can_toggle_restart_production": target_user.can_toggle_restart_production,
            }
        )
    return _respond(request, True, message)


@router.post("/{user_id}/password")
def set_user_password(
    request: Request,
    user_id: int,
    password: str = Form(...),
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    target_user = db.get(User, user_id)
    if target_user is None:
        return _respond(request, False, "User không tồn tại")
    if len(password) < 6:
        return _respond(request, False, "Mật khẩu phải có ít nhất 6 ký tự")
    target_user.password_hash = hash_password(password)
    db.commit()
    return _respond(request, True, f"Đã đặt mật khẩu đăng nhập cho '{target_user.email}'")


@router.post("/{user_id}/staging-toggle")
def toggle_staging_permission(
    request: Request,
    user_id: int,
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    """Cong tac TONG cho phep Admin/User thuong sua replicas Staging (Bat/Tat, doi so pod,
    Sync, Tat tat ca) - KHONG bao gom Restart (xem staging-restart-toggle rieng ben duoi).
    Super Admin luon co quyen mac dinh, khong bi anh huong boi cot nay - xem
    User.can_write_staging."""
    target_user = db.get(User, user_id)
    if target_user is None:
        return _respond(request, False, "User không tồn tại")
    target_user.can_toggle_staging = not target_user.can_toggle_staging
    db.commit()
    state = "bật" if target_user.can_toggle_staging else "tắt"
    if is_ajax(request):
        return JSONResponse(
            {
                "ok": True,
                "message": f"Đã {state} quyền Thay đổi pod Staging cho '{target_user.email}'",
                "can_toggle_staging": target_user.can_toggle_staging,
            }
        )
    return _respond(request, True, f"Đã {state} quyền Thay đổi pod Staging cho '{target_user.email}'")


@router.post("/{user_id}/production-toggle")
def toggle_production_permission(
    request: Request,
    user_id: int,
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    """Cong tac TONG rieng cho quyen sua replicas Production - DOC LAP voi staging-toggle
    o tren. KHONG bao gom Restart (xem production-restart-toggle rieng ben duoi) - xem
    User.can_write_production."""
    target_user = db.get(User, user_id)
    if target_user is None:
        return _respond(request, False, "User không tồn tại")
    target_user.can_toggle_production = not target_user.can_toggle_production
    db.commit()
    state = "bật" if target_user.can_toggle_production else "tắt"
    if is_ajax(request):
        return JSONResponse(
            {
                "ok": True,
                "message": f"Đã {state} quyền thay đổi replicas Production cho '{target_user.email}'",
                "can_toggle_production": target_user.can_toggle_production,
            }
        )
    return _respond(request, True, f"Đã {state} quyền thay đổi replicas Production cho '{target_user.email}'")


@router.post("/{user_id}/staging-view-toggle")
def toggle_staging_view_permission(
    request: Request,
    user_id: int,
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    """Cong tac TONG cho quyen CHI XEM (read-only) menu Staging - DOC LAP voi
    staging-toggle o tren (quyen GHI). Xem User.can_view_staging/can_access_staging/
    can_write_staging."""
    target_user = db.get(User, user_id)
    if target_user is None:
        return _respond(request, False, "User không tồn tại")
    target_user.can_view_staging = not target_user.can_view_staging
    db.commit()
    state = "bật" if target_user.can_view_staging else "tắt"
    if is_ajax(request):
        return JSONResponse(
            {
                "ok": True,
                "message": f"Đã {state} quyền chỉ xem Staging cho '{target_user.email}'",
                "can_view_staging": target_user.can_view_staging,
            }
        )
    return _respond(request, True, f"Đã {state} quyền chỉ xem Staging cho '{target_user.email}'")


@router.post("/{user_id}/production-view-toggle")
def toggle_production_view_permission(
    request: Request,
    user_id: int,
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    """Cong tac TONG cho quyen CHI XEM (read-only) menu Production - DOC LAP voi
    staging-view-toggle o tren (2 quyen tach biet)."""
    target_user = db.get(User, user_id)
    if target_user is None:
        return _respond(request, False, "User không tồn tại")
    target_user.can_view_production = not target_user.can_view_production
    db.commit()
    state = "bật" if target_user.can_view_production else "tắt"
    if is_ajax(request):
        return JSONResponse(
            {
                "ok": True,
                "message": f"Đã {state} quyền chỉ xem Production cho '{target_user.email}'",
                "can_view_production": target_user.can_view_production,
            }
        )
    return _respond(request, True, f"Đã {state} quyền chỉ xem Production cho '{target_user.email}'")


@router.post("/{user_id}/staging-restart-toggle")
def toggle_staging_restart_permission(
    request: Request,
    user_id: int,
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    """Cong tac TONG cho quyen bam nut Restart o menu Staging - TACH BIET hoan toan voi
    staging-toggle (quyen sua replicas) va staging-view-toggle (quyen chi xem). Xem
    User.can_restart_staging."""
    target_user = db.get(User, user_id)
    if target_user is None:
        return _respond(request, False, "User không tồn tại")
    target_user.can_toggle_restart_staging = not target_user.can_toggle_restart_staging
    db.commit()
    state = "bật" if target_user.can_toggle_restart_staging else "tắt"
    if is_ajax(request):
        return JSONResponse(
            {
                "ok": True,
                "message": f"Đã {state} quyền Restart Staging cho '{target_user.email}'",
                "can_toggle_restart_staging": target_user.can_toggle_restart_staging,
            }
        )
    return _respond(request, True, f"Đã {state} quyền Restart Staging cho '{target_user.email}'")


@router.post("/{user_id}/production-restart-toggle")
def toggle_production_restart_permission(
    request: Request,
    user_id: int,
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    """Cong tac TONG cho quyen bam nut Restart o menu Production - tuong tu
    staging-restart-toggle o tren, DOC LAP hoan toan."""
    target_user = db.get(User, user_id)
    if target_user is None:
        return _respond(request, False, "User không tồn tại")
    target_user.can_toggle_restart_production = not target_user.can_toggle_restart_production
    db.commit()
    state = "bật" if target_user.can_toggle_restart_production else "tắt"
    if is_ajax(request):
        return JSONResponse(
            {
                "ok": True,
                "message": f"Đã {state} quyền Restart Production cho '{target_user.email}'",
                "can_toggle_restart_production": target_user.can_toggle_restart_production,
            }
        )
    return _respond(request, True, f"Đã {state} quyền Restart Production cho '{target_user.email}'")
