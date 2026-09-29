"""Kiểm thử app/services/catalog_service.py::sync_catalog_from_chart_repo - đồng bộ
Group/Project từ cấu trúc thư mục charts/{group}/{application}/ (mock git hoàn toàn,
không gọi git CLI/network thật)."""

from sqlalchemy import event

from app.models import CatalogSetting, Group, Project
from app.services import catalog_service


def _make_chart_app(tmp_path, group, application, charts_dir="charts"):
    app_dir = tmp_path / charts_dir / group / application
    app_dir.mkdir(parents=True)
    (app_dir / "Chart.yaml").write_text("name: " + application + "\n")


def _seed_app_setting(db_session, git_remote_repo_url="https://example.com/repo.git"):
    from app.services.app_setting_service import update_app_setting

    update_app_setting(db_session, git_remote_repo_url=git_remote_repo_url)


def _patch_staging_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(catalog_service, "ensure_staging_repo", lambda repo_url: str(tmp_path))


def test_sync_creates_new_groups_and_projects(db_session, monkeypatch, tmp_path):
    _make_chart_app(tmp_path, "core", "api")
    _make_chart_app(tmp_path, "core", "worker")
    _seed_app_setting(db_session)
    _patch_staging_repo(monkeypatch, tmp_path)

    result = catalog_service.sync_catalog_from_chart_repo(db_session)

    assert result["created_groups"] == 1
    assert result["created_projects"] == 2
    group = db_session.query(Group).filter_by(group_name="core").first()
    assert group is not None
    assert {p.application_name for p in group.projects} == {"api", "worker"}


def test_sync_keeps_is_active_and_grants_on_existing_rows(db_session, monkeypatch, tmp_path):
    _make_chart_app(tmp_path, "core", "api")
    _seed_app_setting(db_session)
    _patch_staging_repo(monkeypatch, tmp_path)

    group = Group(group_name="core", is_active=False)
    db_session.add(group)
    db_session.flush()
    project = Project(group_id=group.id, application_name="api", is_active=False)
    db_session.add(project)
    db_session.commit()

    result = catalog_service.sync_catalog_from_chart_repo(db_session)

    assert result["created_groups"] == 0
    assert result["created_projects"] == 0
    db_session.refresh(group)
    db_session.refresh(project)
    assert group.is_active is False  # khong bi dong ve True
    assert project.is_active is False


def test_sync_deletes_groups_and_projects_no_longer_in_repo(db_session, monkeypatch, tmp_path):
    _make_chart_app(tmp_path, "core", "api")
    _seed_app_setting(db_session)
    _patch_staging_repo(monkeypatch, tmp_path)

    stale_group = Group(group_name="removed-group", is_active=True)
    db_session.add(stale_group)
    db_session.flush()
    db_session.add(Project(group_id=stale_group.id, application_name="removed-app", is_active=True))
    db_session.commit()

    result = catalog_service.sync_catalog_from_chart_repo(db_session)

    assert result["deleted_groups"] == 1
    assert result["deleted_projects"] == 1
    assert db_session.query(Group).filter_by(group_name="removed-group").first() is None


def test_sync_excludes_configured_dirs(db_session, monkeypatch, tmp_path):
    _make_chart_app(tmp_path, "core", "api")
    _make_chart_app(tmp_path, "core", "redis")
    _seed_app_setting(db_session)
    _patch_staging_repo(monkeypatch, tmp_path)

    from app.services.app_setting_service import update_app_setting

    update_app_setting(db_session, catalog_excluded_dirs="redis,rabbitmq")

    catalog_service.sync_catalog_from_chart_repo(db_session)

    group = db_session.query(Group).filter_by(group_name="core").first()
    assert {p.application_name for p in group.projects} == {"api"}


def test_sync_query_count_does_not_scale_with_tree_size(db_session, monkeypatch, tmp_path):
    """Regression guard cho N+1 query: truoc day sync_catalog_from_chart_repo chay 1 SELECT
    rieng cho MOI group va MOI project quet duoc trong luc duyet cay thu muc - voi repo
    nhieu group/application, so query tang tuyen tinh theo kich thuoc cay, la nguyen nhan
    chinh khien nut "Sync tu thu muc charts/" cham. Dung kich ban KHONG co gi thay doi
    (moi group/project quet duoc DEU DA co san trong DB, khong insert/delete) de co lap
    dung phan tra cuu (SELECT) khoi so INSERT von di ti le thuan voi so dong moi (khong
    phai bug, khong can toi uu) - sau khi fix, tong so SELECT phai o muc CO DINH (nap 1
    lan qua joinedload), KHONG ti le voi so luong group/application."""
    num_groups, apps_per_group = 8, 6
    for g in range(num_groups):
        group = Group(group_name=f"group{g}", is_active=True)
        db_session.add(group)
        db_session.flush()
        for a in range(apps_per_group):
            _make_chart_app(tmp_path, f"group{g}", f"app{a}")
            db_session.add(Project(group_id=group.id, application_name=f"app{a}", is_active=True))
    db_session.commit()

    _seed_app_setting(db_session)
    _patch_staging_repo(monkeypatch, tmp_path)

    engine = db_session.get_bind()
    query_count = 0

    def _count_queries(conn, cursor, statement, parameters, context, executemany):
        nonlocal query_count
        query_count += 1

    event.listen(engine, "before_cursor_execute", _count_queries)
    try:
        result = catalog_service.sync_catalog_from_chart_repo(db_session)
    finally:
        event.remove(engine, "before_cursor_execute", _count_queries)

    assert result == {"created_groups": 0, "created_projects": 0, "deleted_groups": 0, "deleted_projects": 0}
    # Khong insert/delete gi ca (moi thu da co san) - toan bo query o day la SELECT tra cuu.
    # N+1 truoc day se ra >= 8 (group) + 48 (project) = 56 query. Sau khi fix, chi con vai
    # SELECT co dinh (nap Group+Project 1 lan luc dau, nap lai luc xoa) - nguong 10 du rong
    # nhung van bat duoc regression N+1 that su.
    assert query_count < 10, f"So SELECT = {query_count}, nghi ngo N+1 query quay tro lai"
