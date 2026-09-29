"""Đồng bộ danh mục Group/Project từ cấu trúc thư mục thật của repo Helm charts
(charts/{group}/{application}/) trên nhánh staging, để không phải nhập tay khi
cấu trúc chart thay đổi. Mỗi thư mục cấp 1 trong `charts/` là 1 Group, mỗi thư
mục con bên trong là 1 Application (Project).

Group/Project không còn tồn tại trong repo -> xoá hẳn. Group/Project vẫn còn
tồn tại -> giữ nguyên is_active và mọi quyền (group_admin_access/
project_admin_access) đã gán, không đụng vào."""

from pathlib import Path

from sqlalchemy.orm import Session, joinedload

from app.models import CatalogSetting, Group, Project
from app.services.app_setting_service import get_app_setting
from app.services.git_service import ensure_staging_repo, staging_repo_lock


def get_catalog_setting(db: Session) -> CatalogSetting:
    setting = db.get(CatalogSetting, CatalogSetting.SINGLETON_ID)
    if setting is None:
        setting = CatalogSetting(id=CatalogSetting.SINGLETON_ID, auto_sync_enabled=False)
        db.add(setting)
        db.commit()
        db.refresh(setting)
    return setting


def set_auto_sync_enabled(db: Session, enabled: bool) -> CatalogSetting:
    setting = get_catalog_setting(db)
    setting.auto_sync_enabled = enabled
    db.commit()
    return setting


def _is_application_dir(path: Path) -> bool:
    """Nhan dien thu muc chart application that (co Chart.yaml hoac templates/),
    tranh nhan nham thu muc rac khong phai chart."""
    return (path / "Chart.yaml").exists() or (path / "templates").is_dir()


def sync_catalog_from_chart_repo(db: Session) -> dict:
    app_setting = get_app_setting(db)

    excluded_dirs = {name.strip() for name in app_setting.catalog_excluded_dirs.split(",") if name.strip()}

    seen_groups: set[str] = set()
    seen_projects: dict[str, set[str]] = {}
    created_groups = created_projects = 0

    # Khoa checkout staging dung chung trong suot luc doc cay thu muc charts/ - tranh 1
    # lan verify commit (check_commit_on_staging, xem git_service.py) hoac 1 lan sync
    # khac chay song song reset/clean thu muc nay giua chung, khien ket qua quet cay thu
    # muc bi thieu/sai lech. background=True: ham nay co the chay tu APScheduler (job
    # dinh ky) hoac tu nut "Sync catalog" thu cong, ban than thao tac fetch+reset+clean+
    # quet cay thu muc co the mat vai giay - cho lau hon fail-fast mac dinh cua
    # check_commit_on_staging de tranh fail vo co (xem _GIT_LOCK_TIMEOUT_BACKGROUND_SECONDS).
    with staging_repo_lock(background=True):
        staging_dir = ensure_staging_repo(app_setting.git_remote_repo_url)
        charts_dir = Path(staging_dir) / app_setting.catalog_charts_dir
        if not charts_dir.is_dir():
            raise FileNotFoundError(f"Không tìm thấy thư mục {charts_dir}")

        # Nap toan bo Group + Project hien co vao bo nho 1 LAN (2 query, qua joinedload)
        # thay vi truy van rieng cho tung group/tung project trong luc quet cay thu muc
        # (N+1 query - voi repo nhieu group/application co the len toi hang tram query moi
        # lan sync, la nguyen nhan chinh khien "Sync tu thu muc charts/" cham, ngoai thoi
        # gian git fetch/reset khong the toi uu them). Nap SAU khi da giu lock (khong doi
        # thu tu xin lock so voi truoc) - charts_dir.is_dir() o tren co the raise truoc,
        # dam bao staging_repo_lock() van la thao tac dau tien cua ham.
        existing_groups: dict[str, Group] = {
            g.group_name: g for g in db.query(Group).options(joinedload(Group.projects)).all()
        }
        projects_by_group_id: dict[int, dict[str, Project]] = {
            group.id: {p.application_name: p for p in group.projects} for group in existing_groups.values()
        }

        for group_path in sorted(p for p in charts_dir.iterdir() if p.is_dir()):
            group_name = group_path.name
            seen_groups.add(group_name)

            group = existing_groups.get(group_name)
            if group is None:
                group = Group(group_name=group_name, is_active=True)
                db.add(group)
                db.flush()
                created_groups += 1
                existing_groups[group_name] = group
                projects_by_group_id[group.id] = {}
            # Group da ton tai -> giu nguyen is_active va moi quyen da gan, khong dong tay vao

            seen_projects[group_name] = set()
            group_projects = projects_by_group_id[group.id]
            for app_path in sorted(p for p in group_path.iterdir() if p.is_dir()):
                app_name = app_path.name
                if app_name in excluded_dirs or not _is_application_dir(app_path):
                    continue
                seen_projects[group_name].add(app_name)

                if app_name not in group_projects:
                    db.add(Project(group_id=group.id, application_name=app_name, is_active=True))
                    created_projects += 1
                # Project da ton tai -> giu nguyen is_active va moi quyen da gan, khong dong tay vao

    # Nhóm/application không còn xuất hiện trong repo -> xoá hẳn (ghi đè, lam moi danh muc)
    # joinedload nhu tren - tranh lazy-load group.projects rieng cho tung group (N+1) khi
    # duyet qua toan bo danh sach de tim group/project can xoa.
    deleted_groups = deleted_projects = 0
    for group in db.query(Group).options(joinedload(Group.projects)).all():
        if group.group_name not in seen_groups:
            deleted_projects += len(group.projects)
            db.delete(group)
            deleted_groups += 1
            continue
        for project in list(group.projects):
            if project.application_name not in seen_projects.get(group.group_name, set()):
                db.delete(project)
                deleted_projects += 1

    db.commit()
    return {
        "created_groups": created_groups,
        "created_projects": created_projects,
        "deleted_groups": deleted_groups,
        "deleted_projects": deleted_projects,
    }
