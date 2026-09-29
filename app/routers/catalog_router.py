from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import require_settings_access
from app.config import get_settings
from app.database import get_db
from app.flash import is_ajax, pop_flash, set_flash
from app.models import Group, Project, User
from app.services.app_setting_service import get_app_setting
from app.services.catalog_service import get_catalog_setting, set_auto_sync_enabled, sync_catalog_from_chart_repo
from app.services.git_service import redact_credentials

router = APIRouter(prefix="/catalog", tags=["catalog"])
templates = Jinja2Templates(directory="app/templates")
settings = get_settings()


def _redirect(request: Request, ok: bool, message: str) -> Response:
    if is_ajax(request):
        return JSONResponse({"ok": ok, "message": message})
    set_flash(request, ok, message)
    return RedirectResponse("/catalog", status_code=303)


@router.get("", response_class=HTMLResponse)
def groups_page(request: Request, user: User = Depends(require_settings_access), db: Session = Depends(get_db)):
    groups = db.query(Group).order_by(Group.group_name).all()
    catalog_setting = get_catalog_setting(db)
    flash_ok, flash_msg = pop_flash(request)
    return templates.TemplateResponse(
        "groups.html",
        {
            "request": request,
            "user": user,
            "groups": groups,
            "catalog_charts_dir": get_app_setting(db).catalog_charts_dir,
            "auto_sync_enabled": catalog_setting.auto_sync_enabled,
            "auto_sync_interval_minutes": settings.catalog_auto_sync_interval_minutes,
            "flash_ok": flash_ok,
            "flash_msg": flash_msg,
        },
    )


@router.post("/groups")
def create_group(
    request: Request,
    group_name: str = Form(...),
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    if db.query(Group).filter(Group.group_name == group_name).first():
        return _redirect(request, False, f"Group '{group_name}' đã tồn tại")
    db.add(Group(group_name=group_name, is_active=True))
    db.commit()
    return _redirect(request, True, f"Đã tạo group '{group_name}'")


@router.post("/groups/{group_id}/toggle")
def toggle_group(
    group_id: int, request: Request, user: User = Depends(require_settings_access), db: Session = Depends(get_db)
):
    group = db.get(Group, group_id)
    if group is None:
        if is_ajax(request):
            return JSONResponse({"ok": False, "message": "Group không tồn tại"}, status_code=404)
        return _redirect(request, False, "Group không tồn tại")
    group.is_active = not group.is_active
    db.commit()
    if is_ajax(request):
        return JSONResponse({"ok": True, "is_active": group.is_active})
    return _redirect(request, True, f"Group '{group.group_name}' -> active={group.is_active}")


@router.post("/groups/{group_id}/projects")
def create_project(
    request: Request,
    group_id: int,
    application_name: str = Form(...),
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    group = db.get(Group, group_id)
    if group is None:
        return _redirect(request, False, "Group không tồn tại")
    exists = (
        db.query(Project)
        .filter(Project.group_id == group_id, Project.application_name == application_name)
        .first()
    )
    if exists:
        return _redirect(request, False, f"Application '{application_name}' đã tồn tại trong group này")
    db.add(Project(group_id=group_id, application_name=application_name, is_active=True))
    db.commit()
    return _redirect(request, True, f"Đã tạo application '{application_name}'")


@router.post("/projects/{project_id}/toggle")
def toggle_project(
    project_id: int, request: Request, user: User = Depends(require_settings_access), db: Session = Depends(get_db)
):
    project = db.get(Project, project_id)
    if project is None:
        if is_ajax(request):
            return JSONResponse({"ok": False, "message": "Application không tồn tại"}, status_code=404)
        return _redirect(request, False, "Application không tồn tại")
    project.is_active = not project.is_active
    db.commit()
    if is_ajax(request):
        return JSONResponse({"ok": True, "is_active": project.is_active})
    return _redirect(request, True, f"Application '{project.application_name}' -> active={project.is_active}")


@router.post("/groups/{group_id}/grants")
def grant_full_access(
    request: Request,
    group_id: int,
    email: str = Form(...),
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    group = db.get(Group, group_id)
    if group is None:
        return _redirect(request, False, "Group không tồn tại")
    target_user = db.query(User).filter(User.email == email).first()
    if target_user is None:
        return _redirect(request, False, f"User '{email}' chưa từng đăng nhập vào app, không thể gán quyền")
    if target_user in group.granted_users:
        return _redirect(request, False, f"'{email}' đã có full quyền trên group '{group.group_name}'")
    group.granted_users.append(target_user)
    db.commit()
    return _redirect(
        request, True, f"Đã gán full quyền (Approve/Reject + Run) '{email}' cho group '{group.group_name}'"
    )


@router.post("/groups/{group_id}/grants/{user_id}/revoke")
def revoke_full_access(
    request: Request,
    group_id: int,
    user_id: int,
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    group = db.get(Group, group_id)
    target_user = db.get(User, user_id)
    if group is None or target_user is None:
        return _redirect(request, False, "Group hoặc user không tồn tại")
    if target_user in group.granted_users:
        group.granted_users.remove(target_user)
        db.commit()
    return _redirect(request, True, f"Đã gỡ full quyền '{target_user.email}' khỏi group '{group.group_name}'")


@router.post("/projects/{project_id}/grants")
def grant_full_access_project(
    request: Request,
    project_id: int,
    email: str = Form(...),
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if project is None:
        return _redirect(request, False, "Application không tồn tại")
    target_user = db.query(User).filter(User.email == email).first()
    if target_user is None:
        return _redirect(request, False, f"User '{email}' chưa từng đăng nhập vào app, không thể gán quyền")
    if target_user in project.granted_users:
        return _redirect(request, False, f"'{email}' đã có full quyền trên application '{project.application_name}'")
    project.granted_users.append(target_user)
    db.commit()
    return _redirect(
        request,
        True,
        f"Đã gán full quyền (Approve/Reject + Run) '{email}' cho application '{project.application_name}'",
    )


@router.post("/projects/{project_id}/grants/{user_id}/revoke")
def revoke_full_access_project(
    request: Request,
    project_id: int,
    user_id: int,
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    target_user = db.get(User, user_id)
    if project is None or target_user is None:
        return _redirect(request, False, "Application hoặc user không tồn tại")
    if target_user in project.granted_users:
        project.granted_users.remove(target_user)
        db.commit()
    return _redirect(
        request, True, f"Đã gỡ full quyền '{target_user.email}' khỏi application '{project.application_name}'"
    )


@router.post("/auto-sync/toggle")
def toggle_auto_sync(
    request: Request, user: User = Depends(require_settings_access), db: Session = Depends(get_db)
):
    current = get_catalog_setting(db)
    setting = set_auto_sync_enabled(db, not current.auto_sync_enabled)
    if is_ajax(request):
        return JSONResponse({"ok": True, "is_active": setting.auto_sync_enabled})
    return _redirect(request, True, f"Auto sync catalog -> {'bật' if setting.auto_sync_enabled else 'tắt'}")


@router.post("/sync")
def sync_catalog(request: Request, user: User = Depends(require_settings_access), db: Session = Depends(get_db)):
    try:
        summary = sync_catalog_from_chart_repo(db)
    except Exception as exc:  # noqa: BLE001 - hiển thị lỗi thực tế cho admin xử lý
        # redact_credentials: lop phong thu thu 2 - GitError da tu che credential o nguon,
        # o day che them 1 lan nua phong truong hop 1 exception khac lo URL kem credential.
        return _redirect(request, False, redact_credentials(f"Sync thất bại: {exc}"))
    return _redirect(
        request,
        True,
        f"Sync xong: tạo mới {summary['created_groups']} group, {summary['created_projects']} application"
        f" — xoá {summary['deleted_groups']} group thừa, {summary['deleted_projects']} application thừa",
    )
