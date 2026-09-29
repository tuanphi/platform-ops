import html
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.auth import get_current_user, get_or_create_csrf_token
from app.config import get_settings
from app.database import get_db
from app.flash import pop_flash, set_flash
from app.models import DeployTask, Group, Project, TaskStatus, User
from app.services import notify_service, task_service
from app.services.app_setting_service import get_app_setting
from app.timeutil import format_dmy, gmt7_to_utc, utc_to_gmt7

router = APIRouter(tags=["tasks"])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["gmt7"] = lambda dt: utc_to_gmt7(dt) if dt else None
templates.env.filters["dmy"] = format_dmy
# Global function de task_list.html/task_detail.html goi {{ csrf_token(request) }} truc
# tiep cho cac form POST doi trang thai deploy (Cancel/Reject/Approve/Run/Auto-deploy/Bulk...)
# ma khong phai sua tung handler GET (list_tasks/task_detail) de nhet "csrf_token" vao
# context - "request" von da luon co san trong context cua moi TemplateResponse. Phai dang
# ky them 1 lan nua o actions_router.py (noi render rollback_picker.html) vi moi
# Jinja2Templates() la 1 Environment rieng, khong dung chung globals giua cac module.
templates.env.globals["csrf_token"] = get_or_create_csrf_token
settings = get_settings()

PAGE_SIZE = 10


def _build_page_items(page: int, total_pages: int) -> list[int | None]:
    """Tinh danh sach cac 'o' hien thi trong thanh phan trang rut gon: so nguyen la 1 trang,
    None la dau "..." (ellipsis). Luon hien trang 1 va trang cuoi (total_pages); giua 2 dau
    la 1 "cua so" 3 trang quanh page hien tai, duoc kep (clamp) trong [2, total_pages-1] va
    tu dong noi lien (khong hien "..." ) khi cua so lien ke voi bien - cho ket qua giong het
    3 kich ban demo da duyet:
      - total_pages <= 7: hien het, khong "...".
      - o giua (vd page=7, total=20): 1 ... 6 7 8 ... 20.
      - o dau (vd page=2, total=20): 1 2 3 4 ... 20 (cua so [1,2,3] bi day thanh [2,3,4] do
        kep lower-bound >= 2, roi noi lien voi trang 1 vi lien ke).
      - o cuoi (vd page=19, total=20): 1 ... 17 18 19 20 (doi xung voi truong hop o dau)."""
    if total_pages <= 7:
        return list(range(1, total_pages + 1))

    window_start = page - 1
    window_end = page + 1
    if window_start < 2:
        shift = 2 - window_start
        window_start += shift
        window_end += shift
    if window_end > total_pages - 1:
        shift = window_end - (total_pages - 1)
        window_end -= shift
        window_start -= shift
    window_start = max(window_start, 2)
    window_end = min(window_end, total_pages - 1)

    items: list[int | None] = [1]
    if window_start - 1 > 1:
        items.append(None)
    items.extend(range(window_start, window_end + 1))
    if total_pages - window_end > 1:
        items.append(None)
    items.append(total_pages)
    return items


def _view_scope_filter(user: User) -> ColumnElement[bool]:
    """Dieu kien SQL loc DeployTask theo dung pham vi user KHONG full-access duoc xem:
    task do chinh minh tao, HOAC thuoc 1 Group duoc grant (moi application trong group),
    HOAC thuoc dung 1 Project (group+application) duoc grant le. DeployTask.project luu
    group.group_name va DeployTask.application luu project.application_name (xem
    task_service.create_task) nen so khop truc tiep theo 2 truong nay, dong bo voi
    User.can_view_task/_has_project_grant."""
    conditions: list[ColumnElement[bool]] = [DeployTask.email == user.email]

    granted_group_names = [g.group_name for g in user.granted_groups]
    if granted_group_names:
        conditions.append(DeployTask.project.in_(granted_group_names))

    for project in user.granted_projects:
        conditions.append(
            and_(
                DeployTask.project == project.group.group_name,
                DeployTask.application == project.application_name,
            )
        )

    return or_(*conditions)


def _notify_new_task(task: DeployTask) -> None:
    """email/commitid/updatefor la free-text nguoi dung nhap qua form Tao request -
    PHAI html.escape truoc khi noi suy vao message parse_mode=HTML (Telegram) de tranh
    chen the/link gia mao (vd updatefor="<a href='...'>...</a>")."""
    link = f"{settings.app_url}/tasks/{task.id}"
    escaped_email = html.escape(task.email)
    escaped_commitid = html.escape(task.commitid)
    escaped_updatefor = html.escape(task.updatefor) if task.updatefor else "-"
    message = (
        "🔔 <b>New deploy request</b>\n"
        f"<b>- User:</b> {escaped_email}\n"
        f"<b>- Project:</b> {task.project}\n"
        f"<b>- Application:</b> {task.application}\n"
        f"<b>- CommitID:</b> {escaped_commitid}\n"
        f"<b>- UpdateFor:</b> {escaped_updatefor}\n"
        f"<a href='{link}'>Get more info</a>"
    )
    notify_service.send_telegram(message)
    notify_service.send_mail(
        f"Có request deploy mới từ {escaped_email}: {task.project}/{task.application} ({escaped_commitid})<br>"
        f"<a href='{link}'>Xem chi tiết</a>",
    )


@router.get("/", response_class=HTMLResponse)
def index(request: Request, user: User = Depends(get_current_user)):
    return RedirectResponse("/home", status_code=303)


@router.get("/tasks/create", response_class=HTMLResponse)
def create_form(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    groups = db.query(Group).filter(Group.is_active.is_(True)).order_by(Group.group_name).all()
    groups_data = [{"id": g.id, "group_name": g.group_name} for g in groups]
    flash_ok, flash_msg = pop_flash(request)
    return templates.TemplateResponse(
        "task_form.html",
        {
            "request": request,
            "user": user,
            "groups": groups,
            "groups_data": groups_data,
            "flash_ok": flash_ok,
            "flash_msg": flash_msg,
        },
    )


@router.post("/tasks/create", response_class=HTMLResponse)
def create_submit(
    request: Request,
    background_tasks: BackgroundTasks,
    group_id: int = Form(...),
    application: str = Form(...),
    commitid: str = Form(...),
    config_env: str = Form(""),
    updatefor: str = Form(...),
    confirmed_run_at: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        # confirmed_run_at la chuoi ISO (yyyy-MM-ddThh:mm) da duoc JS o task_form.html
        # doi tu dd/mm/yyyy hh:mm nguoi dung nhap sang, theo gio GMT+7 (VN). Doi ve UTC de
        # luu DB dong nhat voi cac truong datetime khac va scheduler so sanh dung gio.
        run_at = gmt7_to_utc(datetime.fromisoformat(confirmed_run_at))
    except (ValueError, OverflowError):
        set_flash(request, False, "Giờ dự kiến không hợp lệ")
        return RedirectResponse("/tasks/create", status_code=303)

    result = task_service.create_task(
        db, user, group_id, application, commitid, config_env or None, updatefor, run_at
    )
    if result.ok and result.task:
        background_tasks.add_task(_notify_new_task, result.task)
        target = f"/tasks/{result.task.id}"
    else:
        target = "/tasks/create"
    set_flash(request, result.ok, result.message)
    return RedirectResponse(target, status_code=303)


@router.get("/api/groups/{group_id}/projects")
def list_projects_for_group(group_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    projects = (
        db.query(Project)
        .filter(Project.group_id == group_id, Project.is_active.is_(True))
        .order_by(Project.application_name)
        .all()
    )
    return [{"id": p.id, "application_name": p.application_name} for p in projects]


@router.get("/tasks", response_class=HTMLResponse)
def list_tasks(
    request: Request,
    q: str = "",
    status: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(PAGE_SIZE, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(DeployTask)
    # BUOC 1 - pham vi xem: is_full_access_role (Admin/Super Admin) moi duoc xem TOAN BO
    # task. User chi duoc grant Group/Project rieng le (du can_view_all_tasks == True) chi
    # duoc xem dung pham vi cua ho - tranh leo thang view-scope vuot qua action-scope da
    # gioi han dung o can_approve/can_run (fix lai lo hong HIGH: grant 1 project le nhung
    # xem duoc moi task cua moi group/project khac, gom ca config_env nhay cam).
    if not user.is_full_access_role:
        query = query.filter(_view_scope_filter(user))

    # BUOC 2 - tim kiem q: ap dung cho MOI user, CONG THEM (AND) len query da bi gioi han
    # o buoc 1 o tren - TUYET DOI khong dung de thay the _view_scope_filter, tranh lo du
    # lieu ngoai view-scope cho user thuong.
    q = q.strip()
    if q:
        search_conditions: list[ColumnElement[bool]] = [
            DeployTask.project.ilike(f"%{q}%"),
            DeployTask.application.ilike(f"%{q}%"),
            DeployTask.commitid.ilike(f"%{q}%"),
            DeployTask.email.ilike(f"%{q}%"),
            DeployTask.image_version.ilike(f"%{q}%"),
            DeployTask.updatefor.ilike(f"%{q}%"),
        ]
        if q.isdigit():
            # Guard giong task_statuses: q.isdigit() khong dam bao int(q) parse duoc
            # (vd chu so Unicode khong phai ASCII -> ValueError) va khong gioi han do
            # dai chuoi (chuoi so cuc dai -> gia tri vuot BIGINT -> OverflowError o
            # driver DB). Parse an toan, bo qua am tham neu vuot pham vi hop le.
            MAX_ID_VALUE = 2**63 - 1  # gioi han BIGINT signed cua cot id
            try:
                q_as_id = int(q)
            except ValueError:
                q_as_id = None
            if q_as_id is not None and 0 < q_as_id <= MAX_ID_VALUE:
                search_conditions.append(DeployTask.id == q_as_id)
        query = query.filter(or_(*search_conditions))

    # BUOC 3 - status
    if status:
        query = query.filter(DeployTask.status == status)

    total = query.count()
    tasks = (
        query.order_by(DeployTask.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    total_pages = max(1, (total + page_size - 1) // page_size)
    flash_ok, flash_msg = pop_flash(request)
    return templates.TemplateResponse(
        "task_list.html",
        {
            "request": request,
            "user": user,
            "tasks": tasks,
            "q": q,
            "status": status,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "page_items": _build_page_items(page, total_pages),
            "statuses": [s.value for s in TaskStatus],
            "rollback_allowed_seconds": get_app_setting(db).rollback_allowed_seconds,
            "flash_ok": flash_ok,
            "flash_msg": flash_msg,
        },
    )


@router.get("/tasks/api/statuses")
def task_statuses(
    ids: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Endpoint JSON nhe cho JS polling o task_list.html: FE goi dinh ky de biet status
    hien tai cua cac task dang hien thi tren trang, tu do tu quyet dinh co can fetch lai
    ca HTML hay khong - tranh phai reload nguyen trang moi vai giay (nhe tai server/bang
    thong). Parse "ids" (chuoi id cach nhau boi dau phay) mot cach an toan: gia tri rac/
    khong phai so bi bo qua am tham, KHONG raise loi (input tu client khong dang tin cay).
    Gioi han so luong id parse duoc de tranh lam dung xay dung IN() qua lon.

    QUAN TRONG (bao mat): ap dung DUNG view-scope filter y het list_tasks - tai su dung
    _view_scope_filter(user), KHONG viet lai logic loc khac - de tranh IDOR: task ngoai
    pham vi cua user se tu dong bi loai khoi ket qua (im lang), khong bao gio duoc tiet lo
    qua endpoint nay du no ton tai hay khong. Chi tra ve id + status, KHONG kem bat ky
    truong nhay cam nao khac (config_env, email, project...)."""
    MAX_ID_VALUE = 2**63 - 1  # gioi han BIGINT signed cua cot id, tranh OverflowError o driver DB
    parsed_ids: list[int] = []
    for raw in ids.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if value <= 0 or value > MAX_ID_VALUE:
            # id luon duong va phai nam trong pham vi BIGINT hop le - bo qua am tham,
            # khong raise, giong cac gia tri rac khac (tranh 500 OverflowError).
            continue
        parsed_ids.append(value)
        if len(parsed_ids) >= 200:
            break

    if not parsed_ids:
        return []

    query = db.query(DeployTask).filter(DeployTask.id.in_(parsed_ids))
    if not user.is_full_access_role:
        query = query.filter(_view_scope_filter(user))

    return [{"id": t.id, "status": t.status.value} for t in query.all()]


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_detail(task_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    task = db.get(DeployTask, task_id)
    # Ap dung cung dieu kien view-scope nhu list_tasks: chi is_full_access_role
    # (Admin/Super Admin), chu task, hoac user duoc grant dung Group/Project cua task nay
    # (can_view_task) moi duoc xem chi tiet - tranh IDOR doc duoc config_env (co the chua
    # secret) cua task ngoai pham vi duoc cap chi bang cach doi task_id tren URL. Coi nhu
    # "khong tim thay" thay vi 403 de khong lo ton tai cua task_id do cho nguoi khong co
    # quyen.
    if (
        task is not None
        and not user.is_full_access_role
        and task.email != user.email
        and not user.can_view_task(task.project, task.application)
    ):
        task = None
    flash_ok, flash_msg = pop_flash(request)
    return templates.TemplateResponse(
        "task_detail.html",
        {
            "request": request,
            "user": user,
            "task": task,
            "rollback_allowed_seconds": get_app_setting(db).rollback_allowed_seconds,
            "flash_ok": flash_ok,
            "flash_msg": flash_msg,
        },
    )


@router.get("/home", response_class=HTMLResponse)
def home(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    query = db.query(DeployTask)
    if not user.is_full_access_role:
        query = query.filter(_view_scope_filter(user))

    counts = {s.value: query.filter(DeployTask.status == s).count() for s in TaskStatus}

    flash_ok, flash_msg = pop_flash(request)
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "user": user, "counts": counts, "flash_ok": flash_ok, "flash_msg": flash_msg},
    )
