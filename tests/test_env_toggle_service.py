"""Kiểm thử app/services/env_toggle_service.py::sync_apps - đồng bộ cache EnvAppCache từ
git (tạo mới/cập nhật/xoá dòng không còn xuất hiện), giống catalog_service.sync_catalog_from_chart_repo
nhưng cho staging/production."""

from app.models import EnvAppCache
from app.services import env_toggle_service, git_service


def test_sync_apps_creates_new_rows(db_session):
    result = env_toggle_service.sync_apps(
        db_session, "staging", lambda config: [("core", "api", "1"), ("core", "worker", "0")], None
    )

    assert result["created"] == 2
    assert result["updated"] == 0
    assert result["deleted"] == 0
    rows = db_session.query(EnvAppCache).filter_by(env="staging").all()
    assert {(r.project, r.application, r.replicas) for r in rows} == {("core", "api", "1"), ("core", "worker", "0")}


def test_sync_apps_updates_changed_replicas(db_session):
    db_session.add(EnvAppCache(env="staging", project="core", application="api", replicas="0"))
    db_session.commit()

    result = env_toggle_service.sync_apps(db_session, "staging", lambda config: [("core", "api", "3")], None)

    assert result["created"] == 0
    assert result["updated"] == 1
    row = db_session.query(EnvAppCache).filter_by(env="staging", project="core", application="api").first()
    assert row.replicas == "3"


def test_sync_apps_deletes_rows_no_longer_present(db_session):
    db_session.add(EnvAppCache(env="staging", project="core", application="removed-app", replicas="0"))
    db_session.commit()

    result = env_toggle_service.sync_apps(db_session, "staging", lambda config: [], None)

    assert result["deleted"] == 1
    assert db_session.query(EnvAppCache).filter_by(env="staging").count() == 0


def test_sync_apps_does_not_touch_other_env_rows(db_session):
    """Dong bo staging KHONG duoc dung/xoa nham dong cua production (cung 1 bang, khac
    cot env)."""
    db_session.add(EnvAppCache(env="production", project="core", application="api", replicas="5"))
    db_session.commit()

    env_toggle_service.sync_apps(db_session, "staging", lambda config: [("core", "api", "1")], None)

    prod_row = db_session.query(EnvAppCache).filter_by(env="production", project="core", application="api").first()
    assert prod_row.replicas == "5"  # khong bi dung vao


def test_sync_apps_raises_when_list_fn_raises(db_session):
    """sync_apps KHONG tu nuot GitError - de caller (router/scheduler) tu quyet dinh xu
    ly (bao loi cho nguoi dung, hay log+bo qua trong job dinh ky)."""
    from app.services.git_service import GitError

    def _raise(config):
        raise GitError("Repo URL chưa được cấu hình")

    try:
        env_toggle_service.sync_apps(db_session, "staging", _raise, None)
        assert False, "phai raise GitError"
    except GitError:
        pass


# ---------------------------------------------------------------------------
# toggle_app/bulk_toggle - write_through_cache phai bi BO QUA khi ket qua la
# pending_merge_request=True (Production, che do tao Merge Request thay vi push thang) -
# thay doi luc do moi chi nam trong 1 MR cho duyet, chua thuc su ap dung len master.
# ---------------------------------------------------------------------------


def test_toggle_app_writes_through_cache_on_direct_push(db_session, make_user_factory, monkeypatch):
    monkeypatch.setattr(
        "app.services.env_toggle_service.notify_service.send_infra_toggle_telegram", lambda msg, env: None
    )
    db_session.add(EnvAppCache(env="production", project="core", application="api", replicas="0"))
    db_session.commit()
    user = make_user_factory()

    def _toggle_fn(project, application, replicas, actor_email, config):
        return git_service.ReplicasToggleResult(ok=True, message="Đã cập nhật", changed=((project, application),))

    env_toggle_service.toggle_app(
        db_session, user, "core", "api", True, 5, "production", _toggle_fn, None
    )

    row = db_session.query(EnvAppCache).filter_by(env="production", project="core", application="api").first()
    assert row.replicas == "5"


def test_toggle_app_skips_write_through_cache_when_pending_merge_request(db_session, make_user_factory, monkeypatch):
    monkeypatch.setattr(
        "app.services.env_toggle_service.notify_service.send_infra_toggle_telegram", lambda msg, env: None
    )
    db_session.add(EnvAppCache(env="production", project="core", application="api", replicas="0"))
    db_session.commit()
    user = make_user_factory()

    def _toggle_fn(project, application, replicas, actor_email, config):
        return git_service.ReplicasToggleResult(
            ok=True,
            message="Đã tạo Merge Request vào nhánh master",
            changed=((project, application),),
            pending_merge_request=True,
        )

    result = env_toggle_service.toggle_app(
        db_session, user, "core", "api", True, 5, "production", _toggle_fn, None
    )

    assert result.ok is True
    row = db_session.query(EnvAppCache).filter_by(env="production", project="core", application="api").first()
    assert row.replicas == "0"  # KHONG bi ghi de - thay doi moi chi la 1 MR dang cho duyet


def test_bulk_toggle_skips_write_through_cache_when_pending_merge_request(db_session, make_user_factory, monkeypatch):
    monkeypatch.setattr(
        "app.services.env_toggle_service.notify_service.send_infra_toggle_telegram", lambda msg, env: None
    )
    db_session.add(EnvAppCache(env="production", project="core", application="api", replicas="0"))
    db_session.commit()
    user = make_user_factory()

    def _bulk_toggle_fn(apps, enabled, actor_email, config):
        return git_service.ReplicasToggleResult(
            ok=True, message="Đã tạo Merge Request vào nhánh master", changed=tuple(apps), pending_merge_request=True
        )

    env_toggle_service.bulk_toggle(
        db_session, user, [("core", "api")], True, "production", _bulk_toggle_fn, None
    )

    row = db_session.query(EnvAppCache).filter_by(env="production", project="core", application="api").first()
    assert row.replicas == "0"
