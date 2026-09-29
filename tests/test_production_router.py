"""Kiểm thử luồng chính app/routers/production_router.py: phân quyền (can_access_production,
ĐỘC LẬP với can_access_staging) + toggle 1 app / sync thành công. git_service bị mock hoàn
toàn. GET /production đọc từ bảng cache EnvAppCache (env="production") - KHÔNG gọi git lúc
load trang; danh sách được đồng bộ qua POST /production/sync hoặc job định kỳ."""

import re

import pytest

from app.models import EnvAppCache, RestartLog, Role
from app.services import git_service
from tests.conftest import make_user


def make_cache_row(db_session, env="production", project="core", application="api", replicas="0"):
    row = EnvAppCache(env=env, project=project, application=application, replicas=replicas)
    db_session.add(row)
    db_session.commit()
    return row


def _extract_form(html: str, action: str) -> str:
    """Cat rieng 1 khoi <form ... action="{action}" ...> ... </form> - xem docstring day
    du trong tests/test_staging_router.py (mirror)."""
    match = re.search(rf'<form[^>]*action="{re.escape(action)}"[^>]*>.*?</form>', html, re.DOTALL)
    assert match, f"Không tìm thấy <form action=\"{action}\"> trong HTML"
    return match.group(0)


def _stub_notify(monkeypatch):
    monkeypatch.setattr("app.services.env_toggle_service.notify_service.send_infra_toggle_telegram", lambda msg, env: None)


def test_production_page_denied_for_plain_user_without_grant(client_factory, db_session):
    user = make_user(db_session, email="plain@example.com", role=Role.USER)
    client = client_factory(user=user)

    resp = client.get("/production")

    assert resp.status_code == 403


def test_production_page_denied_for_user_with_only_staging_flag(client_factory, db_session):
    """can_toggle_staging KHONG duoc cap quyen truy cap Production - 2 quyen doc lap."""
    user = make_user(db_session, email="staging-only@example.com", role=Role.USER, can_toggle_staging=True)
    client = client_factory(user=user)

    resp = client.get("/production")

    assert resp.status_code == 403


def test_production_page_allowed_for_super_admin(client_factory, db_session):
    sa = make_user(db_session, email="sa@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)

    resp = client.get("/production")

    assert resp.status_code == 200


def test_production_page_allowed_for_user_with_production_flag_granted(client_factory, db_session):
    user = make_user(db_session, email="granted@example.com", role=Role.USER, can_toggle_production=True)
    client = client_factory(user=user)

    resp = client.get("/production")

    assert resp.status_code == 200


def test_production_page_lists_apps_from_cache_not_git(client_factory, db_session, monkeypatch):
    make_cache_row(db_session, env="production", project="core", application="api", replicas="5")

    def _fail(config):
        raise AssertionError("GET /production khong duoc goi git_service.list_production_apps truc tiep")

    monkeypatch.setattr(git_service, "list_production_apps", _fail)

    sa = make_user(db_session, email="sa2@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)

    resp = client.get("/production")

    assert resp.status_code == 200
    assert "5 pod" in resp.text


def test_production_page_only_shows_production_env_rows(client_factory, db_session):
    make_cache_row(db_session, env="production", project="core", application="api", replicas="1")
    make_cache_row(db_session, env="staging", project="core", application="staging-only-app", replicas="1")

    sa = make_user(db_session, email="sa3@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)

    resp = client.get("/production")

    assert "staging-only-app" not in resp.text


# ---------------------------------------------------------------------------
# POST /production/sync
# ---------------------------------------------------------------------------


def test_sync_happy_path_populates_cache_via_master_branch(client_factory, db_session, monkeypatch):
    calls = []
    monkeypatch.setattr(
        git_service,
        "list_production_apps",
        lambda config: (calls.append(config) or [("core", "api", "2")]),
    )
    sa = make_user(db_session, email="syncer@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)

    resp = client.post("/production/sync", headers={"Accept": "application/json"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert len(calls) == 1  # list_production_apps (nhanh master), khong phai list_staging_apps
    row = db_session.query(EnvAppCache).filter_by(env="production").first()
    assert row.project == "core" and row.application == "api" and row.replicas == "2"


def test_sync_denied_for_user_without_permission(client_factory, db_session, monkeypatch):
    monkeypatch.setattr(git_service, "list_production_apps", lambda config: [("core", "api", "1")])
    user = make_user(db_session, email="nosync@example.com", role=Role.USER, can_toggle_production=False)
    client = client_factory(user=user)

    resp = client.post("/production/sync", headers={"Accept": "application/json"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert db_session.query(EnvAppCache).count() == 0


# ---------------------------------------------------------------------------
# POST /production/toggle
# ---------------------------------------------------------------------------


def test_toggle_happy_path_uses_master_branch_and_writes_through_cache(client_factory, db_session, monkeypatch):
    make_cache_row(db_session, env="production", project="core", application="api", replicas="0")
    user = make_user(db_session, email="toggler@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=user)
    monkeypatch.setattr(
        git_service,
        "toggle_production_app",
        lambda project, application, replicas, actor_email, config: git_service.ReplicasToggleResult(
            ok=True, message="ok", changed=((project, application),)
        ),
    )
    _stub_notify(monkeypatch)

    resp = client.post(
        "/production/toggle",
        data={"project": "core", "application": "api", "enabled": "1", "replicas": "5"},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    row = db_session.query(EnvAppCache).filter_by(env="production", project="core", application="api").first()
    assert row.replicas == "5"


def test_toggle_denied_for_user_without_permission(client_factory, db_session, monkeypatch):
    user = make_user(db_session, email="noperm@example.com", role=Role.USER, can_toggle_production=False)
    client = client_factory(user=user)

    resp = client.post(
        "/production/toggle",
        data={"project": "core", "application": "api", "enabled": "1"},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_no_turn_off_all_route_exists_for_production(client_factory, db_session):
    """Xac nhan production KHONG co nut/route Tat tat ca (khac staging, theo yeu cau)."""
    sa = make_user(db_session, email="sa4@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)

    resp = client.post("/production/turn-off-all", headers={"Accept": "application/json"})

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Quyen CHI XEM (can_view_production, read-only) - vao xem duoc trang nhung KHONG sua
# duoc bat ky gi (sync/toggle/bulk-toggle/restart deu phai bi tu choi).
# ---------------------------------------------------------------------------


def test_production_page_allowed_for_view_only_user(client_factory, db_session):
    user = make_user(
        db_session, email="viewer@example.com", role=Role.USER, can_view_production=True, can_toggle_production=False
    )
    client = client_factory(user=user)

    resp = client.get("/production")

    assert resp.status_code == 200


def test_production_page_dims_mutating_controls_for_viewer(client_factory, db_session):
    """User CHI co can_view_production: form Bat/Tat va Restart PHAI VAN duoc render
    (khong an sau "—" nua) nhung nut ben trong PHAI bi disabled (lam mo) - xem
    tests/test_staging_router.py (mirror) cho giai thich day du."""
    make_cache_row(db_session, env="production", project="core", application="api", replicas="1")
    user = make_user(
        db_session, email="viewer2@example.com", role=Role.USER, can_view_production=True, can_toggle_production=False
    )
    client = client_factory(user=user)

    resp = client.get("/production")

    assert resp.status_code == 200
    assert 'action="/production/sync"' not in resp.text
    assert 'id="production-bulk-toolbar"' not in resp.text

    toggle_form = _extract_form(resp.text, "/production/toggle")
    assert "disabled" in toggle_form, "Form Bat/Tat phai render nhung nut ben trong bi lam mo"

    restart_form = _extract_form(resp.text, "/production/restart")
    assert "disabled" in restart_form, "Form Restart phai render nhung nut ben trong bi lam mo"


@pytest.mark.parametrize(
    "path,data",
    [
        ("/production/sync", {}),
        ("/production/toggle", {"project": "core", "application": "api", "enabled": "1"}),
        ("/production/bulk-toggle", {"apps": ["core|api"], "enabled": "1"}),
        ("/production/restart", {"project": "core", "application": "api"}),
    ],
)
def test_view_only_user_cannot_mutate_production(client_factory, db_session, path, data):
    make_cache_row(db_session, env="production", project="core", application="api", replicas="1")
    user = make_user(
        db_session,
        email="viewer3@example.com",
        role=Role.USER,
        can_view_production=True,
        can_toggle_production=False,
    )
    client = client_factory(user=user)

    resp = client.post(path, data=data, headers={"Accept": "application/json"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    row = db_session.query(EnvAppCache).filter_by(env="production", project="core", application="api").first()
    assert row.replicas == "1", "Khong co bat ky thay doi nao duoc ghi cho user chi co quyen doc"


# ---------------------------------------------------------------------------
# Quyen Restart (can_toggle_restart_production) TACH BIET hoan toan voi quyen sua
# Replicas (can_toggle_production) tu 2026-08-18.
# ---------------------------------------------------------------------------


def test_replicas_only_user_cannot_restart_production(client_factory, db_session):
    make_cache_row(db_session, env="production", project="core", application="api", replicas="1")
    user = make_user(
        db_session,
        email="replicas-only@example.com",
        role=Role.USER,
        can_toggle_production=True,
        can_toggle_restart_production=False,
    )
    client = client_factory(user=user)

    resp = client.post(
        "/production/restart",
        data={"project": "core", "application": "api"},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_restart_only_user_can_restart_but_not_edit_replicas_production(client_factory, db_session, monkeypatch):
    make_cache_row(db_session, env="production", project="core", application="api", replicas="1")
    user = make_user(
        db_session,
        email="restart-only@example.com",
        role=Role.USER,
        can_toggle_production=False,
        can_toggle_restart_production=True,
    )
    client = client_factory(user=user)
    monkeypatch.setattr("app.services.argocd_service.settings.argocd_url_api", "")

    resp = client.post(
        "/production/restart",
        data={"project": "core", "application": "api"},
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 200
    assert "Không có quyền" not in resp.json()["message"]
    log = db_session.query(RestartLog).filter_by(env="production", project="core", application="api").first()
    assert log is not None
    assert log.ok is False
    assert "ArgoCD" in log.message

    toggle_resp = client.post(
        "/production/toggle",
        data={"project": "core", "application": "api", "enabled": "1"},
        headers={"Accept": "application/json"},
    )
    assert toggle_resp.json()["ok"] is False
    row = db_session.query(EnvAppCache).filter_by(env="production", project="core", application="api").first()
    assert row.replicas == "1"


def test_production_page_enables_restart_button_but_dims_replicas_controls_for_restart_only_user(
    client_factory, db_session
):
    """User CHI co can_toggle_restart_production: nut Restart PHAI duoc BAT, nut
    Bat/Tat/Luu PHAI bi lam mo - 2 quyen hoan toan doc lap tu 2026-08-18."""
    make_cache_row(db_session, env="production", project="core", application="api", replicas="1")
    user = make_user(
        db_session,
        email="restart-only2@example.com",
        role=Role.USER,
        can_toggle_production=False,
        can_toggle_restart_production=True,
    )
    client = client_factory(user=user)

    resp = client.get("/production")

    assert resp.status_code == 200
    assert 'action="/production/sync"' not in resp.text

    toggle_form = _extract_form(resp.text, "/production/toggle")
    assert "disabled" in toggle_form, "Khong co can_toggle_production nen form Bat/Tat phai bi lam mo"

    restart_form = _extract_form(resp.text, "/production/restart")
    assert "disabled" not in restart_form, "Co can_toggle_restart_production nen nut Restart phai duoc BAT"


# ---------------------------------------------------------------------------
# POST /production/bulk-restart - tuong tu tests/test_staging_router.py (mirror), xem do
# cho docstring day du.
# ---------------------------------------------------------------------------

from app.services import argocd_service  # noqa: E402


class _FakeArgoResponse:
    def __init__(self, json_data=None):
        self._json_data = json_data or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._json_data


def _enable_argocd(monkeypatch):
    monkeypatch.setattr(argocd_service.settings, "argocd_url_api", "https://argocd.example.com/api/v1/applications")
    monkeypatch.setattr(argocd_service.settings, "argocd_token", "fake-token")


def _stub_deployment_tree(monkeypatch):
    tree = {"nodes": [{"kind": "Deployment", "name": "x", "namespace": "ns", "group": "apps", "version": "v1"}]}
    monkeypatch.setattr(argocd_service.httpx, "get", lambda *a, **k: _FakeArgoResponse(tree))
    monkeypatch.setattr(argocd_service.httpx, "post", lambda *a, **k: _FakeArgoResponse({}))


def test_bulk_restart_happy_path_restarts_all_selected_apps(client_factory, db_session, monkeypatch):
    _enable_argocd(monkeypatch)
    _stub_deployment_tree(monkeypatch)
    make_cache_row(db_session, env="production", project="core", application="api", replicas="1")
    make_cache_row(db_session, env="production", project="core", application="worker", replicas="1")
    user = make_user(db_session, email="bulk-restart@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=user)

    resp = client.post(
        "/production/bulk-restart",
        data={"apps": ["core|api", "core|worker"]},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "2" in body["message"]

    logs = db_session.query(RestartLog).filter_by(env="production").all()
    assert len(logs) == 2
    assert all(log.ok for log in logs)


def test_bulk_restart_denied_without_restart_permission(client_factory, db_session, monkeypatch):
    _enable_argocd(monkeypatch)
    called = {"post": False}
    monkeypatch.setattr(argocd_service.httpx, "get", lambda *a, **k: _FakeArgoResponse({}))
    monkeypatch.setattr(argocd_service.httpx, "post", lambda *a, **k: called.__setitem__("post", True) or _FakeArgoResponse({}))
    make_cache_row(db_session, env="production", project="core", application="api", replicas="1")
    user = make_user(
        db_session,
        email="bulk-restart-denied@example.com",
        role=Role.USER,
        can_toggle_production=True,
        can_toggle_restart_production=False,
    )
    client = client_factory(user=user)

    resp = client.post(
        "/production/bulk-restart",
        data={"apps": ["core|api"]},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert called["post"] is False, "Khong duoc goi ArgoCD khi khong co quyen Restart"
