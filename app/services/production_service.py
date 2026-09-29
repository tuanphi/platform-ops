"""Wrapper mong cho tinh nang thay doi replicas Production - business logic dung chung
nam o env_toggle_service.py, o day chi gan file dich (production_values_file_path) + cac
ham git_service tuong ung voi nhanh master (DUNG CHUNG checkout/lock voi Run/Rollback)."""

from sqlalchemy.orm import Session

from app.models import User
from app.services import env_toggle_service, git_service
from app.services.app_setting_service import get_app_setting

ENV_LABEL = "production"


def _push_config(db: Session) -> git_service.EnvReplicasPushConfig:
    app_setting = get_app_setting(db)
    return git_service.EnvReplicasPushConfig(
        values_file_path=app_setting.production_values_file_path,
        catalog_charts_dir=app_setting.catalog_charts_dir,
        catalog_excluded_dirs=app_setting.catalog_excluded_dirs,
        git_commit_author_email=app_setting.git_commit_author_email,
        git_commit_author_name=app_setting.git_commit_author_name,
        git_remote_repo_url=app_setting.git_remote_repo_url,
        use_merge_request=app_setting.production_use_merge_request,
        target_branch=app_setting.git_branch_master,
    )


def toggle_app(
    db: Session, user: User, project: str, application: str, enabled: bool, replicas: int | None
) -> env_toggle_service.EnvActionResult:
    return env_toggle_service.toggle_app(
        db,
        user,
        project,
        application,
        enabled,
        replicas,
        ENV_LABEL,
        git_service.toggle_production_app,
        _push_config(db),
    )


def bulk_toggle(
    db: Session, user: User, apps: list[tuple[str, str]], enabled: bool
) -> env_toggle_service.EnvActionResult:
    return env_toggle_service.bulk_toggle(
        db, user, apps, enabled, ENV_LABEL, git_service.bulk_toggle_production_apps, _push_config(db)
    )


def sync_apps(db: Session) -> dict:
    """Dong bo lai cache tu git (nhanh master) - goi tu nut "Lam moi" (thu cong) hoac
    scheduler job dinh ky (xem app/scheduler.py::job_sync_env_apps)."""
    return env_toggle_service.sync_apps(db, ENV_LABEL, git_service.list_production_apps, _push_config(db))


def list_apps_with_replicas(db: Session) -> list[dict]:
    return env_toggle_service.list_apps_with_replicas(db, ENV_LABEL)
