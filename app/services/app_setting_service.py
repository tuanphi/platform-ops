"""Cau hinh runtime Super Admin sua duoc tu UI /settings/general thay vi .env + restart.

Dong duy nhat trong bang app_setting duoc seed tu Settings (.env) khi doc lan dau;
sau do DB la nguon that su. Cac field nhay cam (token/mat khau) khong nam trong
day, van chi doc tu .env qua get_settings() nhu cu.
"""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models import AppSetting


def _seed_from_env() -> AppSetting:
    s = get_settings()
    # Kep gia tri seed vao [min_hours, max_hours] du .env cau hinh SESSION_REMEMBER_HOURS
    # lech ngoai khoang SESSION_REMEMBER_MIN_HOURS/MAX_HOURS (vd sua .env khong dong bo) -
    # dam bao dong dau tien trong DB luon hop le, khong can validate rieng luc seed.
    session_remember_hours = max(s.session_remember_min_hours, min(s.session_remember_hours, s.session_remember_max_hours))
    return AppSetting(
        id=AppSetting.SINGLETON_ID,
        catalog_charts_dir=s.catalog_charts_dir,
        catalog_excluded_dirs=s.catalog_excluded_dirs,
        chart_values_file_template=s.chart_values_file_template,
        rollback_allowed_seconds=s.rollback_allowed_seconds,
        session_remember_hours=session_remember_hours,
        argocd_url_application=s.argocd_url_application,
        argocd_application_postfix=s.argocd_application_postfix,
        argocd_application_postfix_staging=s.argocd_application_postfix_staging,
        argocd_application_postfix_production=s.argocd_application_postfix_production,
        restart_cooldown_seconds=s.restart_cooldown_seconds,
        telegram_chat_id_system=s.telegram_chat_id_system,
        telegram_chat_id_staging=s.telegram_chat_id_staging,
        telegram_chat_id_production=s.telegram_chat_id_production,
        enable_staging_notify=s.enable_staging_notify,
        enable_production_notify=s.enable_production_notify,
        enable_mail=s.enable_mail,
        enable_scheduler=s.enable_scheduler,
        enable_30min_reminder=s.enable_30min_reminder,
        running_alert_after_minutes=s.running_alert_after_minutes,
        git_commit_author_email=s.git_commit_author_email,
        git_commit_author_name=s.git_commit_author_name,
        git_branch_staging=s.git_branch_staging,
        git_branch_master=s.git_branch_master,
        mail_from=s.mail_from,
        mail_to=s.mail_to,
        mail_subject=s.mail_subject,
        staging_values_file_path=s.staging_values_file_path,
        production_values_file_path=s.production_values_file_path,
        production_use_merge_request=s.production_use_merge_request,
        # git_remote_repo_url: khong seed tu .env (bien cu GIT_REMOTE_APPLICATIONS_URL
        # gop ca credential nen khong the tach tu dong) - bat buoc cau hinh 1 lan qua
        # UI /settings/general sau khi nang cap.
        git_remote_repo_url="",
    )


def get_app_setting(db: Session | None = None) -> AppSetting:
    """Neu khong truyen db (goi tu noi khong san co session, vd notify_service,
    scheduler luc startup) thi tu mo/dong 1 session rieng - AppSetting chi co cot
    scalar nen doc thuoc tinh sau khi session dong van an toan (khong lazy-load)."""
    if db is not None:
        setting = db.get(AppSetting, AppSetting.SINGLETON_ID)
        if setting is None:
            setting = _seed_from_env()
            db.add(setting)
            try:
                db.commit()
            except IntegrityError:
                # 2 request/thread cung phat hien dong singleton chua ton tai va cung
                # INSERT gan nhu dong thoi (vd 2 Run tranh chap cung goi get_app_setting()
                # truoc khi scheduler startup kip seed san) - chi 1 cai thang (UNIQUE
                # constraint tren id), cai thua rollback roi doc lai dong DA duoc tao boi
                # cai thang thay vi de IntegrityError lan thang ra ngoai thanh loi 500.
                db.rollback()
                setting = db.get(AppSetting, AppSetting.SINGLETON_ID)
                if setting is None:
                    raise
                return setting
            db.refresh(setting)
        return setting

    own_db = SessionLocal()
    try:
        return get_app_setting(own_db)
    finally:
        own_db.close()


def update_app_setting(db: Session, **fields) -> AppSetting:
    setting = get_app_setting(db)
    for key, value in fields.items():
        setattr(setting, key, value)
    db.commit()
    db.refresh(setting)
    return setting
