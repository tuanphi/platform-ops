"""Wrapper mong cho tinh nang Bat/Tat staging - business logic dung chung nam o
env_toggle_service.py, o day chi gan file dich (staging_values_file_path) + cac ham
git_service tuong ung voi nhanh staging."""

from sqlalchemy.orm import Session

from app.models import User
from app.services import env_toggle_service, git_service
from app.services.app_setting_service import get_app_setting

ENV_LABEL = "staging"


def _push_config(db: Session) -> git_service.EnvReplicasPushConfig:
    app_setting = get_app_setting(db)
    return git_service.EnvReplicasPushConfig(
        values_file_path=app_setting.staging_values_file_path,
        catalog_charts_dir=app_setting.catalog_charts_dir,
        catalog_excluded_dirs=app_setting.catalog_excluded_dirs,
        git_commit_author_email=app_setting.git_commit_author_email,
        git_commit_author_name=app_setting.git_commit_author_name,
        git_remote_repo_url=app_setting.git_remote_repo_url,
    )


def toggle_app(
    db: Session, user: User, project: str, application: str, enabled: bool, replicas: int | None
) -> env_toggle_service.EnvActionResult:
    return env_toggle_service.toggle_app(
        db, user, project, application, enabled, replicas, ENV_LABEL, git_service.toggle_staging_app, _push_config(db)
    )


def bulk_toggle(
    db: Session, user: User, apps: list[tuple[str, str]], enabled: bool
) -> env_toggle_service.EnvActionResult:
    return env_toggle_service.bulk_toggle(
        db, user, apps, enabled, ENV_LABEL, git_service.bulk_toggle_staging_apps, _push_config(db)
    )


def turn_off_all(db: Session, user: User) -> env_toggle_service.EnvActionResult:
    """Set replicas=0 cho MOI app dang co trong file values-staging - phan quyen (chi
    Admin/Super Admin) da duoc kiem o staging_router truoc khi goi ham nay. CHI staging
    co hanh dong nay (production khong co "Tat tat ca" theo yeu cau)."""
    try:
        result = git_service.turn_off_all_staging(user.email, _push_config(db))
    except git_service.GitError as exc:
        result = git_service.ReplicasToggleResult(ok=False, message=str(exc))

    if result.ok and result.changed:
        env_toggle_service.write_through_cache(db, ENV_LABEL, result.changed, 0)

    env_toggle_service.notify("Tắt tất cả staging", user.email, result, ENV_LABEL)
    return env_toggle_service.EnvActionResult(ok=result.ok, message=result.message)


def sync_apps(db: Session) -> dict:
    """Dong bo lai cache tu git - goi tu nut "Lam moi" (thu cong) hoac scheduler job dinh
    ky (xem app/scheduler.py::job_sync_env_apps)."""
    return env_toggle_service.sync_apps(db, ENV_LABEL, git_service.list_staging_apps, _push_config(db))


def list_apps_with_replicas(db: Session) -> list[dict]:
    return env_toggle_service.list_apps_with_replicas(db, ENV_LABEL)
