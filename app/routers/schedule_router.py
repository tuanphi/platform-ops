"""Menu Schedule: hen gio tu dong Restart / Tat-Bat app tren staging/production. Xem
app/services/schedule_service.py cho business logic va cach thuc thi (job moi phut trong
app/scheduler.py::job_run_app_schedules)."""

from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import check_csrf_token, get_current_user, get_or_create_csrf_token
from app.database import get_db
from app.flash import is_ajax, pop_flash, set_flash
from app.models import EnvAppCache, User
from app.services import schedule_service
from app.timeutil import format_dmy, utc_to_gmt7

router = APIRouter(prefix="/schedule", tags=["schedule"])
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["csrf_token"] = get_or_create_csrf_token
templates.env.filters["gmt7"] = lambda dt: utc_to_gmt7(dt) if dt else None
templates.env.filters["dmy"] = format_dmy

_TABS = {"restart": ("restart",), "toggle": ("stop", "start")}
_CSRF_MESSAGE = "Phiên làm việc đã hết hạn, vui lòng tải lại trang và thử lại"


def _tab_for(action: str) -> str:
    return "restart" if action == "restart" else "toggle"


def _redirect(request: Request, tab: str, ok: bool, message: str) -> RedirectResponse:
    set_flash(request, ok, message)
    return RedirectResponse(f"/schedule?tab={tab}", status_code=303)


def _require_access(user: User) -> None:
    if not user.can_access_schedule:
        raise HTTPException(status_code=403, detail="Permission Denied")


@router.get("/status")
def schedule_status(
    tab: str = "restart", user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Trang /schedule goi dinh ky (30s) de cap nhat cot "Lan chay toi"/"Lan chay gan nhat"
    sau khi job tu chay lich - chi DOC bang app_schedule, khong goi git/ArgoCD."""
    _require_access(user)
    actions = _TABS.get(tab, _TABS["restart"])
    return JSONResponse(
        [
            {
                "id": s.id,
                "is_active": s.is_active,
                "next_run_at": format_dmy(utc_to_gmt7(s.next_run_at)) if s.next_run_at else None,
                "last_run_at": format_dmy(utc_to_gmt7(s.last_run_at)) if s.last_run_at else None,
                "last_ok": s.last_ok,
                "last_skipped": bool(s.last_skipped),
                "last_message": s.last_message or "",
                "finished": schedule_service.is_finished(s),
            }
            for s in schedule_service.list_schedules(db, user, actions)
        ]
    )


@router.get("", response_class=HTMLResponse)
def schedule_page(
    request: Request, tab: str = "restart", user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    _require_access(user)
    # Chi hien tab user co quyen xem (Lich Restart -> tab restart, Lich Tat/Bat -> tab
    # tat/bat, View -> ca 2); vao thang tab khong co quyen thi chuyen sang tab xem duoc.
    visible_tabs = [t for t, acts in _TABS.items() if schedule_service.visible_envs(user, acts[0])]
    if tab not in visible_tabs:
        tab = visible_tabs[0] if visible_tabs else "restart"
    actions = _TABS[tab]
    # env nao user duoc TAO lich cho tab hien tai (restart: quyen Restart; toggle: quyen
    # sua replicas) - dropdown Moi truong chi hien cac env nay.
    create_envs = [env for env in schedule_service.ENVS if schedule_service.can_manage(user, env, actions[0])]
    apps: dict[str, dict[str, list[str]]] = {env: {} for env in create_envs}
    if create_envs:
        rows = (
            db.query(EnvAppCache)
            .filter(EnvAppCache.env.in_(create_envs))
            .order_by(EnvAppCache.project, EnvAppCache.application)
            .all()
        )
        for row in rows:
            apps[row.env].setdefault(row.project, []).append(row.application)

    schedules = [
        {
            "s": s,
            "describe": schedule_service.describe(s),
            "can_manage": schedule_service.can_manage(user, s.env, s.action),
            "finished": schedule_service.is_finished(s),
        }
        for s in schedule_service.list_schedules(db, user, actions)
    ]
    flash_ok, flash_msg = pop_flash(request)
    return templates.TemplateResponse(
        "schedule.html",
        {
            "request": request,
            "user": user,
            "tab": tab,
            "visible_tabs": visible_tabs,
            "create_envs": create_envs,
            "apps_data": apps,
            "schedules": schedules,
            "actions": schedule_service.ACTIONS,
            "frequencies": schedule_service.FREQUENCIES,
            "weekdays": schedule_service.WEEKDAYS,
            "flash_ok": flash_ok,
            "flash_msg": flash_msg,
        },
    )


def _parse_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _form_fields(
    action: str = Form(...),
    env: str = Form(...),
    project: str = Form(""),
    application: str = Form(""),
    frequency: str = Form(...),
    time_of_day: str = Form(""),
    weekday: str = Form(""),
    day_of_month: str = Form(""),
    run_once_at: str = Form(""),
) -> dict:
    """Doc chung form tao/sua lich. run_once_at sai dinh dang -> giu chuoi loi de router
    tra thong bao (xem _save)."""
    run_once_local: datetime | str | None = None
    if frequency == "once":
        try:
            run_once_local = datetime.fromisoformat(run_once_at)
        except ValueError:
            run_once_local = "invalid"
    return {
        "action": action,
        "env": env,
        "project": project,
        "application": application,
        "frequency": frequency,
        "time_of_day": time_of_day,
        "weekday": _parse_int(weekday),
        "day_of_month": _parse_int(day_of_month),
        "run_once_at_local": run_once_local,
    }


def _save(request: Request, csrf_token: str, user: User, form: dict, save_fn) -> RedirectResponse:
    _require_access(user)
    tab = _tab_for(form["action"])
    if not check_csrf_token(request, csrf_token):
        return _redirect(request, tab, False, _CSRF_MESSAGE)
    if form["run_once_at_local"] == "invalid":
        return _redirect(request, tab, False, "Ngày giờ chạy không hợp lệ (dd/mm/yyyy hh:mm)")
    result = save_fn(**form)
    return _redirect(request, tab, result.ok, result.message)


@router.post("/create")
def create(
    request: Request,
    form: dict = Depends(_form_fields),
    apps: list[str] = Form([]),
    csrf_token: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Tao lich cho NHIEU app 1 luc: moi item `apps` dang "project|application" (form chon
    nhieu app). Van nhan cap project/application don le (tuong thich cach goi cu)."""
    parsed = [tuple(item.split("|", 1)) for item in apps if "|" in item]
    project, application = form.pop("project"), form.pop("application")
    if not parsed and project and application:
        parsed = [(project, application)]
    return _save(
        request, csrf_token, user, form, lambda **f: schedule_service.create_schedules(db, user, parsed, **f)
    )


@router.post("/{schedule_id}/edit")
def edit(
    request: Request,
    schedule_id: int,
    form: dict = Depends(_form_fields),
    csrf_token: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _save(
        request, csrf_token, user, form, lambda **f: schedule_service.update_schedule(db, user, schedule_id, **f)
    )


@router.post("/{schedule_id}/delete")
def delete(
    request: Request,
    schedule_id: int,
    tab: str = Form("restart"),
    csrf_token: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_access(user)
    tab = tab if tab in _TABS else "restart"
    if not check_csrf_token(request, csrf_token):
        return _redirect(request, tab, False, _CSRF_MESSAGE)
    result = schedule_service.delete_schedule(db, user, schedule_id)
    return _redirect(request, tab, result.ok, result.message)


@router.post("/bulk-edit")
def bulk_edit(
    request: Request,
    form: dict = Depends(_form_fields),
    ids: list[int] = Form([]),
    csrf_token: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Sua nhieu lich da tich chon: ap dung hanh dong/tan suat/gio moi, giu nguyen moi
    truong + app cua tung lich."""
    return _save(request, csrf_token, user, form, lambda **f: schedule_service.bulk_edit(db, user, ids, **f))


@router.post("/bulk")
def bulk(
    request: Request,
    op: str = Form(...),
    ids: list[int] = Form([]),
    tab: str = Form("restart"),
    csrf_token: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Tam dung / bat lai / xoa nhieu lich da tich chon tren danh sach."""
    _require_access(user)
    tab = tab if tab in _TABS else "restart"
    if not check_csrf_token(request, csrf_token):
        return _redirect(request, tab, False, _CSRF_MESSAGE)
    result = schedule_service.bulk_update(db, user, ids, op)
    return _redirect(request, tab, result.ok, result.message)


@router.post("/{schedule_id}/active")
def set_active(
    request: Request,
    schedule_id: int,
    active: str = Form(...),
    tab: str = Form("restart"),
    csrf_token: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Nut ⏸/▶ tren dong - trang goi bang fetch (Accept: application/json) de cap nhat tai
    cho, khong reload (giu vi tri cuon/trang/loc/o da tich); form submit thuong van redirect."""
    _require_access(user)
    tab = tab if tab in _TABS else "restart"
    if not check_csrf_token(request, csrf_token):
        result = schedule_service.ScheduleResult(False, _CSRF_MESSAGE)
    else:
        result = schedule_service.set_active(db, user, schedule_id, active == "1")
    if is_ajax(request):
        return JSONResponse({"ok": result.ok, "message": result.message})
    return _redirect(request, tab, result.ok, result.message)
