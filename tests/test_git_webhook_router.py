"""Kiểm thử app/routers/git_webhook_router.py - webhook nhận GitLab push event, trigger
sync catalog/staging/production khi push đúng nhánh staging/master đang cấu hình.

Router tự mở/đóng session bằng SessionLocal() (không qua Depends(get_db)) nên không dùng
lại được conftest.client_factory - monkeypatch git_webhook_router.SessionLocal sang
db_session của test, giống cách tests/test_telegram_router.py đã làm."""

import pytest
from fastapi.testclient import TestClient

from app.services.git_service import GitError

WEBHOOK_SECRET = "test-git-webhook-secret"


@pytest.fixture()
def webhook_client(db_session, monkeypatch):
    import app.main as main_module
    from app.routers import git_webhook_router

    monkeypatch.setattr(main_module, "start_scheduler", lambda: None)
    monkeypatch.setattr(main_module, "shutdown_scheduler", lambda: None)

    monkeypatch.setattr(git_webhook_router, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(git_webhook_router.settings, "git_webhook_secret", WEBHOOK_SECRET)

    calls = []
    monkeypatch.setattr(
        git_webhook_router.catalog_service, "sync_catalog_from_chart_repo", lambda db: calls.append("catalog")
    )
    monkeypatch.setattr(git_webhook_router.staging_service, "sync_apps", lambda db: calls.append("staging"))
    monkeypatch.setattr(git_webhook_router.production_service, "sync_apps", lambda db: calls.append("production"))

    client = TestClient(main_module.app)
    client.calls = calls  # type: ignore[attr-defined]
    return client


def _post(client, ref, secret=WEBHOOK_SECRET):
    headers = {}
    if secret is not None:
        headers["x-gitlab-token"] = secret
    return client.post("/webhook/git", json={"ref": ref}, headers=headers)


def test_rejects_when_secret_not_configured(db_session, monkeypatch):
    import app.main as main_module
    from app.routers import git_webhook_router

    monkeypatch.setattr(main_module, "start_scheduler", lambda: None)
    monkeypatch.setattr(main_module, "shutdown_scheduler", lambda: None)
    monkeypatch.setattr(git_webhook_router, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(git_webhook_router.settings, "git_webhook_secret", "")

    client = TestClient(main_module.app)
    res = _post(client, "refs/heads/staging", secret="anything")

    assert res.status_code == 403


def test_rejects_wrong_secret(webhook_client):
    res = _post(webhook_client, "refs/heads/staging", secret="wrong-secret")

    assert res.status_code == 403
    assert webhook_client.calls == []


def test_rejects_missing_header(webhook_client):
    res = _post(webhook_client, "refs/heads/staging", secret=None)

    assert res.status_code == 403
    assert webhook_client.calls == []


def test_push_to_staging_branch_syncs_catalog_and_staging_only(webhook_client):
    res = _post(webhook_client, "refs/heads/staging")

    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert set(webhook_client.calls) == {"catalog", "staging"}


def test_push_to_master_branch_syncs_production_only(webhook_client):
    res = _post(webhook_client, "refs/heads/master")

    assert res.status_code == 200
    assert webhook_client.calls == ["production"]


def test_push_to_unrelated_branch_syncs_nothing(webhook_client):
    res = _post(webhook_client, "refs/heads/production-replicas-20260802-100000")

    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert webhook_client.calls == []


def test_push_tag_ref_is_ignored(webhook_client):
    res = _post(webhook_client, "refs/tags/v1.0.0")

    assert res.status_code == 200
    assert webhook_client.calls == []


def test_missing_ref_is_ignored(webhook_client):
    res = webhook_client.post("/webhook/git", json={}, headers={"x-gitlab-token": WEBHOOK_SECRET})

    assert res.status_code == 200
    assert webhook_client.calls == []


def test_sync_error_does_not_fail_the_webhook_request(db_session, monkeypatch):
    import app.main as main_module
    from app.routers import git_webhook_router

    monkeypatch.setattr(main_module, "start_scheduler", lambda: None)
    monkeypatch.setattr(main_module, "shutdown_scheduler", lambda: None)
    monkeypatch.setattr(git_webhook_router, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(git_webhook_router.settings, "git_webhook_secret", WEBHOOK_SECRET)

    def _raise(db):
        raise GitError("Repo URL chưa được cấu hình")

    monkeypatch.setattr(git_webhook_router.catalog_service, "sync_catalog_from_chart_repo", _raise)
    monkeypatch.setattr(git_webhook_router.staging_service, "sync_apps", _raise)

    client = TestClient(main_module.app)
    res = _post(client, "refs/heads/staging")

    assert res.status_code == 200
    assert res.json()["ok"] is True


def test_publishes_sync_event_only_for_successful_syncs(db_session, monkeypatch):
    import app.main as main_module
    from app.routers import git_webhook_router

    monkeypatch.setattr(main_module, "start_scheduler", lambda: None)
    monkeypatch.setattr(main_module, "shutdown_scheduler", lambda: None)
    monkeypatch.setattr(git_webhook_router, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(git_webhook_router.settings, "git_webhook_secret", WEBHOOK_SECRET)

    published = []
    monkeypatch.setattr(git_webhook_router.sync_events, "publish", lambda source: published.append(source))
    monkeypatch.setattr(git_webhook_router.catalog_service, "sync_catalog_from_chart_repo", lambda db: None)

    def _raise(db):
        raise GitError("Repo URL chưa được cấu hình")

    monkeypatch.setattr(git_webhook_router.staging_service, "sync_apps", _raise)

    client = TestClient(main_module.app)
    _post(client, "refs/heads/staging")

    # catalog sync thanh cong -> publish; staging sync loi -> KHONG publish.
    assert published == ["catalog"]
