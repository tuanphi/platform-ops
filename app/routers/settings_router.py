"""Cau hinh chung (General): cho Super Admin sua truc tiep tu UI cac gia tri truoc day
phai sua .env + restart app - xem AppSetting trong models.py de biet field nao doc/ghi
o day va field nao (token/mat khau, AUTH_DEV_MODE/AUTH_ENABLE_PASSWORD, ARGOCD_URL_API)
van chi doc tu .env.

Moi card trong settings_general.html la 1 <form> rieng, tu submit doc lap (nut Luu
rieng cho tung nhom, khong phai cuon xuong cuoi trang) - phan biet bang hidden field
"section", chi cap nhat dung cac cot thuoc section do, khong dung Form(...) bat buoc
cho ca 17 field nhu truoc (se 422 vi cac field cua section khac khong duoc gui len).

Form submit qua fetch() (giong nut gat trong groups.html) de hien thong bao ngay tai
cho, khong load lai trang - tranh nhay ve dau trang moi lan bam Luu mot section o
duoi cung. Form khong-JS van fallback ve redirect + flash tren dau trang nhu cu."""

from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import scheduler as scheduler_module
from app.auth import require_settings_access
from app.config import get_settings
from app.database import get_db
from app.flash import is_ajax, pop_flash, set_flash
from app.models import User
from app.scheduler import apply_scheduler_setting
from app.services.app_setting_service import get_app_setting, update_app_setting

router = APIRouter(prefix="/settings", tags=["settings"])
templates = Jinja2Templates(directory="app/templates")

SECTION_LABELS = {
    "catalog": "Catalog",
    "git": "Git repo",
    "rollback": "Rollback",
    "auth": "Đăng nhập",
    "argocd": "ArgoCD",
    "notify": "Thông báo",
    "scheduler": "Scheduler",
}


def _respond(request: Request, ok: bool, message: str) -> Response:
    if is_ajax(request):
        return JSONResponse({"ok": ok, "message": message})
    set_flash(request, ok, message)
    return RedirectResponse("/settings/general", status_code=303)


def _url_has_credentials(url: str) -> bool:
    """Repo URL o day khong duoc chua credential (dang user:token@host) - credential
    rieng nam trong GIT_REMOTE_CREDENTIALS (.env), tranh nhap nham token vao 1 field
    sua duoc qua UI ma ai co quyen Super Admin cung xem/sua duoc."""
    parsed = urlsplit(url)
    return bool(parsed.username or parsed.password)


def _parse_optional_int(raw: str | None, field_label: str) -> int | None:
    """Parse 1 o so nhan tu Form (khai bao str|None, KHONG dung Form(int|None)) thanh
    int, tra None neu rong/chi co khoang trang. Raise ValueError voi thong bao tieng
    Viet neu chuoi khong phai so nguyen hop le (vd chu, so thap phan) - goi noi nay
    thay vi de FastAPI tu validate kieu int o tang Form, vi FastAPI se tra 422 voi
    body {"detail": [...]} mac dinh (loi ky thuat tho, khong tieng Viet, khac format
    {ok, message} dang dung o moi cho khac trong router nay - xem _respond)."""
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError:
        raise ValueError(f"{field_label} phải là số nguyên hợp lệ") from None


@router.get("/general", response_class=HTMLResponse)
def general_page(request: Request, user: User = Depends(require_settings_access), db: Session = Depends(get_db)):
    settings = get_settings()
    flash_ok, flash_msg = pop_flash(request)
    return templates.TemplateResponse(
        "settings_general.html",
        {
            "request": request,
            "user": user,
            "setting": get_app_setting(db),
            "flash_ok": flash_ok,
            "flash_msg": flash_msg,
            # Nguong min/max cua o "so gio ghi nho dang nhap" doc tu .env (env-only,
            # khong luu app_setting) - xem app/config.py Settings.session_remember_min_hours/max_hours.
            "session_remember_min_hours": settings.session_remember_min_hours,
            "session_remember_max_hours": settings.session_remember_max_hours,
        },
    )


@router.post("/general")
def update_general(
    request: Request,
    section: str = Form(...),
    catalog_charts_dir: str | None = Form(None),
    catalog_excluded_dirs: str | None = Form(None),
    chart_values_file_template: str | None = Form(None),
    git_remote_repo_url: str | None = Form(None),
    git_commit_author_email: str | None = Form(None),
    git_commit_author_name: str | None = Form(None),
    git_branch_staging: str | None = Form(None),
    git_branch_master: str | None = Form(None),
    staging_values_file_path: str | None = Form(None),
    production_values_file_path: str | None = Form(None),
    production_use_merge_request: str | None = Form(None),
    # 4 field so ben duoi khai bao str|None (KHONG phai int|None) de tu parse + validate
    # trong than ham (xem _parse_optional_int) thay vi de FastAPI validate kieu o tang
    # Form - tranh 422 tho khi nguoi dung nhap chu, dong nhat voi cach validate so am
    # da co san (vd max_concurrent_running_tasks < 0).
    rollback_allowed_seconds: str | None = Form(None),
    max_concurrent_running_tasks: str | None = Form(None),
    running_alert_repeat_minutes: str | None = Form(None),
    session_remember_hours: str | None = Form(None),
    argocd_url_application: str | None = Form(None),
    argocd_application_postfix: str | None = Form(None),
    argocd_application_postfix_staging: str | None = Form(None),
    argocd_application_postfix_production: str | None = Form(None),
    restart_cooldown_seconds: str | None = Form(None),
    running_alert_after_minutes: str | None = Form(None),
    telegram_chat_id_system: str | None = Form(None),
    telegram_chat_id_staging: str | None = Form(None),
    telegram_chat_id_production: str | None = Form(None),
    enable_staging_notify: str | None = Form(None),
    enable_production_notify: str | None = Form(None),
    mail_from: str | None = Form(None),
    mail_to: str | None = Form(None),
    mail_subject: str | None = Form(None),
    enable_mail: str | None = Form(None),
    enable_scheduler: str | None = Form(None),
    enable_30min_reminder: str | None = Form(None),
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    if section not in SECTION_LABELS:
        raise HTTPException(status_code=400, detail="Section không hợp lệ")

    if section == "catalog":
        if not (catalog_charts_dir or "").strip() or not (chart_values_file_template or "").strip():
            return _respond(request, False, "Thư mục charts và template file version không được để trống")
        update_app_setting(
            db,
            catalog_charts_dir=catalog_charts_dir.strip(),
            catalog_excluded_dirs=(catalog_excluded_dirs or "").strip(),
            chart_values_file_template=chart_values_file_template.strip(),
        )

    elif section == "git":
        if (
            not (git_remote_repo_url or "").strip()
            or not (git_commit_author_email or "").strip()
            or not (git_branch_staging or "").strip()
            or not (git_branch_master or "").strip()
            or not (staging_values_file_path or "").strip()
            or not (production_values_file_path or "").strip()
        ):
            return _respond(
                request,
                False,
                "Repo URL, Email tác giả commit, nhánh staging/master và đường dẫn file values-staging/values-production không được để trống",
            )
        if _url_has_credentials(git_remote_repo_url.strip()):
            return _respond(request, False, "Repo URL không hợp lệ")

        repo_url = git_remote_repo_url.strip()
        branch_staging = git_branch_staging.strip()
        branch_master = git_branch_master.strip()

        from app.services.git_service import (
            GitError,
            remote_branch_exists,
            validate_safe_branch_name,
            validate_safe_repo_url,
        )

        # Chan CWE-88 (git argument/option injection): whitelist ky tu hop le cua ten nhanh
        # va scheme/ky tu dau cua repo URL NGAY TRUOC khi dua vao bat ky subprocess git nao
        # (remote_branch_exists ben duoi goi `git ls-remote`) - 1 ten nhanh/URL bat dau bang
        # "-" (vd "--upload-pack=...") co the bi git parse nham thanh option CLI, hoac 1
        # scheme nguy hiem (file://, ext::...) co the doc file/thuc thi lenh tuy y. Validate
        # CA BA gia tri truoc, khong luu, khong chay subprocess neu bat ky gia tri nao khong
        # hop le.
        try:
            validate_safe_branch_name(branch_staging, "staging")
            validate_safe_branch_name(branch_master, "master")
            validate_safe_repo_url(repo_url, "git_remote_repo_url")
        except GitError as exc:
            return _respond(request, False, str(exc))

        # Xac nhan CA HAI nhanh thuc su ton tai tren remote truoc khi luu - tranh Super
        # Admin go nham ten nhanh, khien Run/Rollback sau nay moi phat hien loi (luc do
        # ensure_staging_repo/ensure_master_repo moi clone/fetch va fail).
        for label, branch in (("staging", branch_staging), ("master", branch_master)):
            try:
                exists = remote_branch_exists(repo_url, branch)
            except GitError as exc:
                return _respond(request, False, f"Không kiểm tra được nhánh trên remote: {exc}")
            if not exists:
                return _respond(request, False, f"Nhánh '{branch}' ({label}) không tồn tại trên remote")

        update_app_setting(
            db,
            git_remote_repo_url=repo_url,
            git_commit_author_email=git_commit_author_email.strip(),
            git_commit_author_name=(git_commit_author_name or "").strip(),
            git_branch_staging=branch_staging,
            git_branch_master=branch_master,
            staging_values_file_path=staging_values_file_path.strip(),
            production_values_file_path=production_values_file_path.strip(),
            production_use_merge_request=production_use_merge_request is not None,
        )

    elif section == "rollback":
        try:
            rollback_allowed_seconds_val = _parse_optional_int(
                rollback_allowed_seconds, "Thời gian cho phép Rollback"
            )
            max_concurrent_running_tasks_val = _parse_optional_int(
                max_concurrent_running_tasks, "Số task tối đa chạy cùng lúc"
            )
            # Field moi them sau, khong Form(...) bat buoc o tang HTTP (giu tuong thich
            # nguoc voi caller/test cu goi section "rollback" ma chua biet field nay) - neu
            # KHONG gui len (raw rong/None) thi GIU NGUYEN gia tri dang luu, khac 2 field
            # tren (bat buoc phai gui, thieu la loi).
            running_alert_repeat_minutes_val = _parse_optional_int(
                running_alert_repeat_minutes, "Khoảng cách cảnh báo lặp lại"
            )
        except ValueError as exc:
            return _respond(request, False, str(exc))
        if rollback_allowed_seconds_val is None or rollback_allowed_seconds_val <= 0:
            return _respond(request, False, "Thời gian cho phép Rollback phải lớn hơn 0")
        # So nguyen >= 0 (0 = khong gioi han) - khong duoc chi tin thuoc tinh min cua input
        # HTML (co the bi bo qua/sua bang DevTools hoac goi thang API).
        if max_concurrent_running_tasks_val is None or max_concurrent_running_tasks_val < 0:
            return _respond(request, False, "Số task tối đa chạy cùng lúc phải là số nguyên >= 0 (0 = không giới hạn)")
        if running_alert_repeat_minutes_val is None:
            running_alert_repeat_minutes_val = get_app_setting(db).running_alert_repeat_minutes
        # So nguyen >= 0 (0 = chi canh bao 1 lan duy nhat) - cung nguyen tac voi
        # max_concurrent_running_tasks o tren.
        elif running_alert_repeat_minutes_val < 0:
            return _respond(
                request, False, "Khoảng cách cảnh báo lặp lại phải là số nguyên >= 0 (0 = chỉ cảnh báo một lần)"
            )
        update_app_setting(
            db,
            rollback_allowed_seconds=rollback_allowed_seconds_val,
            max_concurrent_running_tasks=max_concurrent_running_tasks_val,
            running_alert_repeat_minutes=running_alert_repeat_minutes_val,
        )

    elif section == "auth":
        # Validate server-side theo nguong doc tu .env (SESSION_REMEMBER_MIN_HOURS/MAX_HOURS,
        # env-only - khong dua len UI/DB) - khong duoc chi tin thuoc tinh min/max cua input
        # HTML (co the bi bo qua/sua bang DevTools hoac goi thang API).
        env_settings = get_settings()
        remember_min = env_settings.session_remember_min_hours
        remember_max = env_settings.session_remember_max_hours
        try:
            session_remember_hours_val = _parse_optional_int(session_remember_hours, "Số giờ ghi nhớ đăng nhập")
        except ValueError as exc:
            return _respond(request, False, str(exc))
        if session_remember_hours_val is None or not (remember_min <= session_remember_hours_val <= remember_max):
            return _respond(
                request,
                False,
                f"Số giờ ghi nhớ đăng nhập phải trong khoảng {remember_min}-{remember_max}",
            )
        update_app_setting(db, session_remember_hours=session_remember_hours_val)

    elif section == "argocd":
        try:
            running_alert_after_minutes_val = _parse_optional_int(
                running_alert_after_minutes, "Ngưỡng cảnh báo Running"
            )
            restart_cooldown_seconds_val = _parse_optional_int(
                restart_cooldown_seconds, "Rate limit khi Restart"
            )
        except ValueError as exc:
            return _respond(request, False, str(exc))
        if running_alert_after_minutes_val is None or running_alert_after_minutes_val <= 0:
            return _respond(request, False, "Ngưỡng cảnh báo Running phải lớn hơn 0")
        if restart_cooldown_seconds_val is None or restart_cooldown_seconds_val <= 0:
            return _respond(request, False, "Rate limit khi Restart phải lớn hơn 0")
        update_app_setting(
            db,
            argocd_url_application=(argocd_url_application or "").strip(),
            argocd_application_postfix=(argocd_application_postfix or "").strip(),
            argocd_application_postfix_staging=(argocd_application_postfix_staging or "").strip(),
            argocd_application_postfix_production=(argocd_application_postfix_production or "").strip(),
            restart_cooldown_seconds=restart_cooldown_seconds_val,
            running_alert_after_minutes=running_alert_after_minutes_val,
        )

    elif section == "notify":
        if not (mail_from or "").strip() or not (mail_subject or "").strip():
            return _respond(request, False, "Mail from và Tiêu đề mail không được để trống")
        update_app_setting(
            db,
            telegram_chat_id_system=(telegram_chat_id_system or "").strip(),
            telegram_chat_id_staging=(telegram_chat_id_staging or "").strip(),
            telegram_chat_id_production=(telegram_chat_id_production or "").strip(),
            enable_staging_notify=enable_staging_notify is not None,
            enable_production_notify=enable_production_notify is not None,
            mail_from=mail_from.strip(),
            mail_to=(mail_to or "").strip(),
            mail_subject=mail_subject.strip(),
            enable_mail=enable_mail is not None,
        )

    elif section == "scheduler":
        update_app_setting(
            db,
            enable_scheduler=enable_scheduler is not None,
            enable_30min_reminder=enable_30min_reminder is not None,
        )
        # Chi dong bo lai cac job PHU (nhac gio, ArgoCD, canh bao, auto deploy, auto sync
        # catalog) theo AppSetting.enable_scheduler vua luu - KHONG dung, khong khoi dong
        # lai toan bo scheduler o day, vi job_process_task_queue (worker xu ly hang doi
        # Run/Rollback) phai luon chay bat ke cong tac nay (xem app/scheduler.py).
        apply_scheduler_setting()
        # apply_scheduler_setting() tu log WARNING neu scheduler khong chay, nhung nguoi
        # bam nut tren UI khong thay duoc log server - bao them ngay trong message tra ve
        # (van giu ok=True vi gia tri DA duoc luu dung vao DB, chi CHUA co hieu luc len
        # job/worker) de admin biet ngay thay vi tuong nham "Da luu" la moi thu da ap dung.
        if not scheduler_module.scheduler.running:
            return _respond(
                request,
                True,
                f"Đã lưu {SECTION_LABELS[section]} vào DB, nhưng scheduler hiện KHÔNG chạy nên "
                "job/worker CHƯA nhận cấu hình mới (kể cả worker Run/Rollback) - báo DevOps kiểm tra ngay",
            )

    return _respond(request, True, f"Đã lưu {SECTION_LABELS[section]}")
