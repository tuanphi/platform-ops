import logging
import threading
import time
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import check_csrf_token, get_current_user, get_or_create_csrf_token
from app.config import get_settings
from app.database import get_db
from app.flash import is_ajax, set_flash
from app.models import DeployTask, User
from app.services import notify_service, task_service
from app.services.app_setting_service import get_app_setting
from app.timeutil import format_dmy, gmt7_to_utc, utc_to_gmt7

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tasks", tags=["actions"])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["dmy"] = format_dmy
templates.env.filters["gmt7"] = lambda dt: utc_to_gmt7(dt) if dt else None
# Global function (khong phai context variable) de template goi truc tiep
# {{ csrf_token(request) }} ma khong phai sua tung handler de nhet "csrf_token" vao
# context - "request" von da luon co san trong context cua moi TemplateResponse (yeu cau
# bat buoc cua Starlette Jinja2Templates). Dung chung 1 ham voi tasks_router.py (noi render
# task_list.html/task_detail.html) - moi Jinja2Templates() la 1 Environment rieng nen phai
# dang ky global nay o CA HAI noi.
templates.env.globals["csrf_token"] = get_or_create_csrf_token
settings = get_settings()


# ---------------------------------------------------------------------------
# Rate limit / cooldown CHI CON cho Rollback (KHONG con ap dung cho Run/bulk Run) - bo
# sung cung dot voi ban vá fail-fast cua git_service._repo_lock (xem
# _GIT_LOCK_TIMEOUT_SECONDS).
#
# LICH SU: ban dau cooldown nay khoa theo EMAIL (dung chung cho ca Run/bulk Run/Rollback),
# nhung gay bug that trong production - user bam Run task #34 xong, vai giay sau bam Run
# task #35 (2 task KHAC NHAU, hop le) thi bi chan oan 429 "thao tac qua nhanh" chi vi
# cung 1 email vua goi Run truoc do. Da xac minh: run_task() (task_service.py) da dung
# CAS (compare-and-swap) doi status sang RUNNING nguyen tu truoc khi push git - 2 request
# Run dong thoi CUNG 1 task chi 1 cai thang, cai thua nhan CONFLICT_MESSAGE_TEMPLATE; con
# 2 request Run tren 2 task KHAC NHAU von di khong tranh chap gi nhau (khong cung khoa git
# nao, KHONG can chan). Vi vay bo hang cooldown nay cho Run don le va bulk Run - CAS san co
# da du bao ve chong spam/double-run tren CUNG 1 task, khong can lop chan bo sung.
#
# Rollback thi KHAC: rollback_task() (task_service.py) KHONG doi status cua task nguon,
# moi lan goi INSERT 1 DeployTask MOI hoan toan -> KHONG co CAS bao ve khoi 2 request
# Rollback dong thoi tren CUNG 1 task nguon (xem docstring rollback_task). Nen GIU LAI
# cooldown cho Rollback, nhung doi khoa tu EMAIL sang (EMAIL, TASK_ID) - vua chan duoc
# spam Rollback lap lai tren CUNG 1 task (lo hong that su), vua KHONG con chan oan viec 1
# user Rollback 2 task KHAC NHAU gan nhau (giong ly do bo cooldown o Run o tren).
#
# Dung 1 dict in-process + threading.Lock la du (khong can Redis/DB rieng) vi app chi
# chay DUNG 1 process uvicorn (bin/run khong truyen --workers) - moi state nay khong can
# chia se giua nhieu process/container nhu file lock cua git_service.
_RUN_ROLLBACK_COOLDOWN_SECONDS = 3.0
_RATE_LIMIT_STATE_MAX_ENTRIES = 5000  # nguong "don dep" cac khoa cu, tranh dict phinh to vo han qua nhieu ngay uptime

_run_rollback_last_call_lock = threading.Lock()
_run_rollback_last_call: dict[tuple[str, int], float] = {}


def _check_rollback_rate_limit(user_email: str, task_id: int) -> bool:
    """Tra True neu duoc phep tiep tuc thao tac Rollback task_id nay ngay bay gio (va ghi
    nhan thoi diem nay lam moc cooldown ke tiep), False neu user nay vua goi Rollback
    CUNG task_id nay qua gan day (con trong cua so cooldown) - khong duoc thao tac tiep,
    tang so lan goi KHONG lam reset lai cooldown (khong tinh tram, chi khoa dua tren lan
    goi DUOC CHAP NHAN gan nhat). Khoa theo cap (email, task_id) - Rollback 2 task khac
    nhau CUNG luc KHONG bi chan nhau (xem comment o dinh file)."""
    now = time.monotonic()
    key = (user_email, task_id)
    with _run_rollback_last_call_lock:
        if len(_run_rollback_last_call) > _RATE_LIMIT_STATE_MAX_ENTRIES:
            # Don dep don gian: bo cac khoa khong hoat dong qua 1 gio - tranh dict phinh
            # to vo han qua nhieu ngay/thang uptime (danh sach nhan vien + task noi bo huu
            # han nen trong dieu kien binh thuong nhanh nhat se khong bao gio cham nguong nay).
            stale_before = now - 3600
            for stale_key in [k for k, ts in _run_rollback_last_call.items() if ts < stale_before]:
                del _run_rollback_last_call[stale_key]

        last_call = _run_rollback_last_call.get(key)
        if last_call is not None and (now - last_call) < _RUN_ROLLBACK_COOLDOWN_SECONDS:
            return False
        _run_rollback_last_call[key] = now
        return True


_RATE_LIMIT_MESSAGE = (
    f"Bạn đang thao tác quá nhanh, vui lòng chờ {int(_RUN_ROLLBACK_COOLDOWN_SECONDS)} giây rồi thử lại"
)


def _is_safe_next(next_url: str | None) -> bool:
    """Chi cho phep redirect ve path noi bo (tranh open-redirect), khong phai URL ngoai."""
    return bool(next_url) and next_url.startswith("/") and not next_url.startswith("//")


# Thong bao than thien khi CSRF token thieu/sai/het han - KHONG lo chi tiet ky thuat
# (khong nhac "CSRF"/"token") ra ngoai, dong nhat voi _RATE_LIMIT_MESSAGE va cac message
# khac trong file nay deu la cau tieng Viet than thien nguoi dung.
_CSRF_ERROR_MESSAGE = "Phiên làm việc đã hết hạn, vui lòng tải lại trang rồi thử lại"


def _redirect_back(
    request: Request, task_id: int, ok: bool, message: str, next_url: str | None, status_code: int = 200
) -> Response:
    if is_ajax(request):
        return JSONResponse({"ok": ok, "message": message}, status_code=status_code)
    base = next_url if _is_safe_next(next_url) else f"/tasks/{task_id}"
    set_flash(request, ok, message)
    return RedirectResponse(base, status_code=303)


def _get_task_or_none(db: Session, task_id: int) -> DeployTask | None:
    return db.get(DeployTask, task_id)


def _redirect_list(
    request: Request, ok: bool, message: str, next_url: str | None, status_code: int = 200
) -> Response:
    if is_ajax(request):
        return JSONResponse({"ok": ok, "message": message}, status_code=status_code)
    base = next_url if _is_safe_next(next_url) else "/tasks"
    set_flash(request, ok, message)
    return RedirectResponse(base, status_code=303)


def _csrf_fail_back(request: Request, task_id: int, next_url: str | None) -> Response:
    """Tra ve loi CSRF cho cac route don le (/tasks/{task_id}/...) - dung 403 (thay vi 200
    mac dinh cua _redirect_back) cho nhanh AJAX de FE/monitoring phan biet duoc voi loi
    nghiep vu binh thuong (vd 'ban khong co quyen')."""
    return _redirect_back(request, task_id, False, _CSRF_ERROR_MESSAGE, next_url, status_code=403)


def _csrf_fail_list(request: Request, next_url: str | None) -> Response:
    """Tra ve loi CSRF cho cac route bulk (/tasks/bulk/...) - xem _csrf_fail_back."""
    return _redirect_list(request, False, _CSRF_ERROR_MESSAGE, next_url, status_code=403)


MAX_BULK_TASK_IDS = 50


def _bulk_action(
    db: Session,
    background_tasks: BackgroundTasks,
    task_ids: list[int],
    user: User,
    verb: str,
    icon: str,
    by_label: str,
    can_act,
    act,
    notify: bool = True,
) -> tuple[bool, str]:
    """Ap dung 1 action (reject/approve/run) len nhieu task, gop lai thanh 1 thong bao ket qua.

    `notify=False` (chi dung cho verb="run"): KHONG gui Telegram "Run request" ngay tai
    day nua - tu khi co hang doi (task_service.run_task chi CAS sang QUEUED, KHONG con goi
    git ngay), task CHUA thuc su chay nen gui "Task is Running" o day se sai su that. Worker
    (app/scheduler.py::job_process_task_queue) se gui thong bao dung luc task THAT SU bat
    dau chay."""
    if len(task_ids) > MAX_BULK_TASK_IDS:
        return False, f"Chỉ được chọn tối đa {MAX_BULK_TASK_IDS} task mỗi lần thao tác"

    ok_count = 0
    errors: list[str] = []
    for task_id in task_ids:
        task = _get_task_or_none(db, task_id)
        if task is None:
            errors.append(f"#{task_id}: không tồn tại")
            continue
        if not can_act(task):
            errors.append(f"#{task_id}: bạn không có quyền {verb}")
            continue
        result = act(task_id)
        if result.ok and result.task:
            ok_count += 1
            if notify:
                message = f"{icon} <b>{verb.capitalize()} request</b>\n" + notify_service.task_summary_lines(
                    result.task, by_label, user.email
                )
                background_tasks.add_task(notify_service.send_telegram, message)
        else:
            errors.append(f"#{task_id}: {result.message}")

    summary = f"Đã {verb} {ok_count}/{len(task_ids)} task"
    if errors:
        summary += " (lỗi: " + "; ".join(errors) + ")"
    return ok_count > 0, summary


@router.post("/bulk/reject")
def bulk_reject(
    request: Request,
    background_tasks: BackgroundTasks,
    task_ids: list[int] = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    next: str | None = Form(None),
    csrf_token: str = Form(""),
):
    if not check_csrf_token(request, csrf_token):
        return _csrf_fail_list(request, next)
    ok, message = _bulk_action(
        db,
        background_tasks,
        task_ids,
        user,
        verb="reject",
        icon="❌",
        by_label="Rejected by",
        can_act=lambda task: user.can_approve(task.project, task.application),
        act=lambda task_id: task_service.reject_task(db, task_id, user),
    )
    return _redirect_list(request, ok, message, next)


@router.post("/bulk/approve")
def bulk_approve(
    request: Request,
    background_tasks: BackgroundTasks,
    task_ids: list[int] = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    next: str | None = Form(None),
    csrf_token: str = Form(""),
):
    if not check_csrf_token(request, csrf_token):
        return _csrf_fail_list(request, next)
    ok, message = _bulk_action(
        db,
        background_tasks,
        task_ids,
        user,
        verb="approve",
        icon="🆗",
        by_label="Approved by",
        can_act=lambda task: user.can_approve(task.project, task.application),
        act=lambda task_id: task_service.approve_task(db, task_id, user),
    )
    return _redirect_list(request, ok, message, next)


@router.post("/bulk/run")
def bulk_run(
    request: Request,
    background_tasks: BackgroundTasks,
    task_ids: list[int] = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    next: str | None = Form(None),
    csrf_token: str = Form(""),
):
    if not check_csrf_token(request, csrf_token):
        return _csrf_fail_list(request, next)
    # KHONG con rate limit cho bulk Run (xem comment o dinh file) - CAS trong
    # task_service.run_task da bao ve du cho tung task, va cac task trong 1 lan bulk von
    # da khac nhau nen khong tranh chap gi voi nhau.
    ok, message = _bulk_action(
        db,
        background_tasks,
        task_ids,
        user,
        verb="run",
        icon="🚀",
        by_label="Run by",
        can_act=lambda task: user.can_run(task.project, task.application),
        act=lambda task_id: task_service.run_task(db, task_id, user),
        notify=False,
    )
    return _redirect_list(request, ok, message, next)


def _can_cancel(user: User, task: DeployTask) -> bool:
    """Nguoi tao task tu huy request cua chinh minh, hoac ai co quyen Approve (Admin/Super
    Admin/duoc grant) thi cung dong duoc, giong pham vi cua Reject."""
    return task.email == user.email or user.can_approve(task.project, task.application)


@router.post("/bulk/cancel")
def bulk_cancel(
    request: Request,
    background_tasks: BackgroundTasks,
    task_ids: list[int] = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    next: str | None = Form(None),
    csrf_token: str = Form(""),
):
    if not check_csrf_token(request, csrf_token):
        return _csrf_fail_list(request, next)
    ok, message = _bulk_action(
        db,
        background_tasks,
        task_ids,
        user,
        verb="cancel",
        icon="🚫",
        by_label="Cancelled by",
        can_act=lambda task: _can_cancel(user, task),
        act=lambda task_id: task_service.cancel_task(db, task_id, user),
    )
    return _redirect_list(request, ok, message, next)


@router.post("/bulk/delete")
def bulk_delete(
    request: Request,
    task_ids: list[int] = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    next: str | None = Form(None),
    csrf_token: str = Form(""),
):
    """Xoa vinh vien - chi Super Admin (user.can_delete_task), rieng biet voi _bulk_action
    (khong gui Telegram cho hanh dong xoa)."""
    if not check_csrf_token(request, csrf_token):
        return _csrf_fail_list(request, next)
    if not user.can_delete_task:
        return _redirect_list(request, False, "Bạn không có quyền Xóa task", next)
    if len(task_ids) > MAX_BULK_TASK_IDS:
        return _redirect_list(request, False, f"Chỉ được chọn tối đa {MAX_BULK_TASK_IDS} task mỗi lần thao tác", next)

    ok_count = 0
    errors: list[str] = []
    for task_id in task_ids:
        result = task_service.delete_task(db, task_id, user)
        if result.ok:
            ok_count += 1
        else:
            errors.append(f"#{task_id}: {result.message}")

    summary = f"Đã xóa {ok_count}/{len(task_ids)} task"
    if errors:
        summary += " (lỗi: " + "; ".join(errors) + ")"
    return _redirect_list(request, ok_count > 0, summary, next)


@router.post("/{task_id}/cancel")
def cancel(
    request: Request,
    task_id: int,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    next: str | None = Form(None),
    csrf_token: str = Form(""),
):
    if not check_csrf_token(request, csrf_token):
        return _csrf_fail_back(request, task_id, next)
    task = _get_task_or_none(db, task_id)
    if task is None or not _can_cancel(user, task):
        return _redirect_back(request, task_id, False, "Bạn không có quyền Cancel request này", next)

    result = task_service.cancel_task(db, task_id, user)
    if result.ok and result.task:
        message = "🚫 <b>Cancel request</b>\n" + notify_service.task_summary_lines(
            result.task, "Cancelled by", user.email
        )
        background_tasks.add_task(notify_service.send_telegram, message)
    return _redirect_back(request, task_id, result.ok, result.message, next)


@router.post("/{task_id}/reject")
def reject(
    request: Request,
    task_id: int,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    next: str | None = Form(None),
    csrf_token: str = Form(""),
):
    if not check_csrf_token(request, csrf_token):
        return _csrf_fail_back(request, task_id, next)
    task = _get_task_or_none(db, task_id)
    if task is None or not user.can_approve(task.project, task.application):
        return _redirect_back(request, task_id, False, "Bạn không có quyền Reject request này", next)

    result = task_service.reject_task(db, task_id, user)
    if result.ok and result.task:
        message = "❌ <b>Reject request</b>\n" + notify_service.task_summary_lines(
            result.task, "Rejected by", user.email
        )
        background_tasks.add_task(notify_service.send_telegram, message)
    return _redirect_back(request, task_id, result.ok, result.message, next)


@router.post("/{task_id}/approve")
def approve(
    request: Request,
    task_id: int,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    next: str | None = Form(None),
    csrf_token: str = Form(""),
):
    if not check_csrf_token(request, csrf_token):
        return _csrf_fail_back(request, task_id, next)
    task = _get_task_or_none(db, task_id)
    if task is None or not user.can_approve(task.project, task.application):
        return _redirect_back(request, task_id, False, "Bạn không có quyền Approve request này", next)

    result = task_service.approve_task(db, task_id, user)
    if result.ok and result.task:
        message = "🆗 <b>Approve request</b>\n" + notify_service.task_summary_lines(
            result.task, "Approved by", user.email
        )
        background_tasks.add_task(notify_service.send_telegram, message)
        run_at_text = (
            f"nhắc chạy lúc {utc_to_gmt7(result.task.confirmed_run_at)} (giờ VN)"
            if result.task.confirmed_run_at is not None
            else "chưa đặt giờ nhắc chạy"
        )
        background_tasks.add_task(
            notify_service.send_mail,
            f"Task {result.task.project}/{result.task.application} đã được approve, {run_at_text}",
        )
    return _redirect_back(request, task_id, result.ok, result.message, next)


@router.post("/{task_id}/run")
def run(
    request: Request,
    task_id: int,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    next: str | None = Form(None),
    csrf_token: str = Form(""),
):
    if not check_csrf_token(request, csrf_token):
        return _csrf_fail_back(request, task_id, next)
    task = _get_task_or_none(db, task_id)
    if task is None or not user.can_run(task.project, task.application):
        return _redirect_back(request, task_id, False, "Bạn không có quyền Run request này", next)

    # KHONG con rate limit cho Run don le (xem comment o dinh file) - CAS trong
    # task_service.run_task (WHERE status IN RUNNABLE_STATUSES) da bao ve nguyen tu, 2
    # request Run CUNG task nay chi 1 cai thang; Run 2 task KHAC NHAU von khong tranh
    # chap gi nen khong can chan.
    #
    # KHONG con gui Telegram "Task is Running" tai day: run_task() gio chi CAS task sang
    # QUEUED (khong con goi git ngay - xem TaskStatus.QUEUED trong models.py), task CHUA
    # thuc su chay nen gui thong bao nay se sai su that. Worker rieng (app/scheduler.py::
    # job_process_task_queue) se gui thong bao dung luc task THAT SU bat dau chay.
    result = task_service.run_task(db, task_id, user)
    return _redirect_back(request, task_id, result.ok, result.message, next)


@router.post("/{task_id}/auto-deploy")
def auto_deploy(
    request: Request,
    task_id: int,
    enabled: str = Form(...),
    auto_run_at: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    next: str | None = Form(None),
    csrf_token: str = Form(""),
):
    if not check_csrf_token(request, csrf_token):
        return _csrf_fail_back(request, task_id, next)
    task = _get_task_or_none(db, task_id)
    if task is None:
        return _redirect_back(request, task_id, False, "Task không tồn tại", next)

    is_enabled = enabled == "1"
    # Dung pham vi nhu Approve (Admin tro len, hoac user duoc grant rieng theo
    # project/application) cho ca bat va tat, doc lap voi pham vi can_run - de nguoi
    # bat Auto deploy (thuong la nguoi Approve task) cung la nguoi co the tu tat lai,
    # khong phu thuoc viec ho co duoc Run task nay hay khong.
    if not user.can_approve(task.project, task.application):
        return _redirect_back(request, task_id, False, "Bạn không có quyền đặt Auto deploy cho request này", next)

    run_at_utc = None
    if is_enabled:
        if not auto_run_at:
            return _redirect_back(request, task_id, False, "Cần chọn thời gian Auto deploy", next)
        try:
            run_at_utc = gmt7_to_utc(datetime.fromisoformat(auto_run_at))
        except (ValueError, OverflowError):
            return _redirect_back(request, task_id, False, "Thời gian Auto deploy không hợp lệ", next)

    result = task_service.set_auto_deploy(db, task_id, user, is_enabled, run_at_utc)
    return _redirect_back(request, task_id, result.ok, result.message, next)


@router.get("/{task_id}/rollback", response_class=HTMLResponse)
def rollback_picker(
    request: Request,
    task_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    next: str | None = None,
):
    task = _get_task_or_none(db, task_id)
    if task is None or not user.can_run(task.project, task.application) or task.status.value != "Done":
        return _redirect_back(request, task_id, False, "Không thể rollback task này", next)
    rollback_allowed_seconds = get_app_setting(db).rollback_allowed_seconds
    if not task.is_rollback_window_open(rollback_allowed_seconds):
        return _redirect_back(
            request, task_id, False, f"Task đã Done quá {rollback_allowed_seconds} giây, không thể rollback nữa", next
        )

    candidates = task_service.list_rollback_candidates(db, task)
    return templates.TemplateResponse(
        "rollback_picker.html",
        {"request": request, "user": user, "task": task, "candidates": candidates, "next_url": next},
    )


@router.post("/{task_id}/rollback")
def rollback(
    request: Request,
    task_id: int,
    background_tasks: BackgroundTasks,
    target_task_id: int = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    next: str | None = Form(None),
    csrf_token: str = Form(""),
):
    if not check_csrf_token(request, csrf_token):
        return _csrf_fail_back(request, task_id, next)
    task = _get_task_or_none(db, task_id)
    if task is None or not user.can_run(task.project, task.application):
        return _redirect_back(request, task_id, False, "Bạn không có quyền Rollback request này", next)

    # Rate limit theo (email, task_id) (xem comment o dinh file) - rollback_task KHONG co
    # CAS bao ve (khong doi status task nguon, chi INSERT dong moi) nen can cooldown rieng
    # chan spam Rollback lap lai tren CUNG 1 task nay; Rollback task KHAC van khong bi anh huong.
    if not _check_rollback_rate_limit(user.email, task_id):
        logger.warning("Rate limit chan Rollback task #%s cho user '%s' - thao tac qua nhanh", task_id, user.email)
        return _redirect_back(request, task_id, False, _RATE_LIMIT_MESSAGE, next, status_code=429)

    result = task_service.rollback_task(db, task_id, target_task_id, user)
    if result.ok and result.task:
        message = "⏪ <b>Rollback</b>\n" + notify_service.task_summary_lines(result.task)
        background_tasks.add_task(notify_service.send_telegram, message)
        # rollback tao task moi -> khong co "next" thi ve trang chi tiet cua task MOI, khong phai task cu
        return _redirect_back(request, result.task.id, True, result.message, next)
    return _redirect_back(request, task_id, False, result.message, next)
