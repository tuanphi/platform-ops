"""Kiểm thử luồng chính app/routers/staging_router.py: phân quyền (can_access_staging /
can_turn_off_all_staging) + toggle 1 app / bulk-toggle / turn-off-all / sync thành công.
git_service bị mock hoàn toàn (KHÔNG chạm git/network thật). GET /staging đọc từ bảng
cache EnvAppCache (KHÔNG gọi git lúc load trang - xem env_toggle_service.py); danh sách
được đồng bộ vào cache qua POST /staging/sync (nút "Làm mới") hoặc job định kỳ."""

import re

import pytest

from app.models import EnvAppCache, RestartLog, Role
from app.services import git_service
from tests.conftest import make_user


def make_cache_row(db_session, env="staging", project="core", application="api", replicas="0"):
    row = EnvAppCache(env=env, project=project, application=application, replicas=replicas)
    db_session.add(row)
    db_session.commit()
    return row


def _extract_form(html: str, action: str) -> str:
    """Cat rieng 1 khoi <form ... action="{action}" ...> ... </form> tu HTML tra ve - dung
    de kiem tra button ben trong co bi "lam mo" (disabled) hay khong, thay vi assert
    action co/khong co mat trong response (form gio LUON duoc render, chi khac o co
    disabled hay khong - xem staging.html/production.html)."""
    match = re.search(rf'<form[^>]*action="{re.escape(action)}"[^>]*>.*?</form>', html, re.DOTALL)
    assert match, f"Không tìm thấy <form action=\"{action}\"> trong HTML"
    return match.group(0)


def _stub_toggle_ok(monkeypatch):
    monkeypatch.setattr(
        git_service,
        "toggle_staging_app",
        lambda project, application, replicas, actor_email, config: git_service.ReplicasToggleResult(
            ok=True, message="Đã cập nhật", changed=((project, application),)
        ),
    )


def _stub_notify(monkeypatch):
    monkeypatch.setattr("app.services.env_toggle_service.notify_service.send_infra_toggle_telegram", lambda msg, env: None)


# ---------------------------------------------------------------------------
# GET /staging - phan quyen + doc tu cache DB (khong goi git)
# ---------------------------------------------------------------------------


def test_staging_page_denied_for_plain_user_without_grant(client_factory, db_session):
    user = make_user(db_session, email="plain@example.com", role=Role.USER)
    client = client_factory(user=user)

    resp = client.get("/staging")

    assert resp.status_code == 403


def test_staging_page_allowed_for_super_admin_without_flag(client_factory, db_session):
    """Super Admin luon full quyen bat ke can_toggle_staging."""
    sa = make_user(db_session, email="sa@example.com", role=Role.SUPER_ADMIN, can_toggle_staging=False)
    client = client_factory(user=sa)

    resp = client.get("/staging")

    assert resp.status_code == 200


def test_staging_page_allowed_for_user_with_flag_granted(client_factory, db_session):
    user = make_user(db_session, email="granted@example.com", role=Role.USER, can_toggle_staging=True)
    client = client_factory(user=user)

    resp = client.get("/staging")

    assert resp.status_code == 200


def test_staging_page_lists_apps_from_cache_not_git(client_factory, db_session, monkeypatch):
    """Danh sach app hien thi PHAI den tu bang cache EnvAppCache - GET khong duoc goi
    git_service.list_staging_apps (se raise neu bi goi nham)."""
    make_cache_row(db_session, env="staging", project="core", application="api", replicas="3")
    make_cache_row(db_session, env="staging", project="core", application="worker", replicas="0")

    def _fail(config):
        raise AssertionError("GET /staging khong duoc goi git_service.list_staging_apps truc tiep")

    monkeypatch.setattr(git_service, "list_staging_apps", _fail)

    sa = make_user(db_session, email="sa2@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)

    resp = client.get("/staging")

    assert resp.status_code == 200
    assert "core" in resp.text and "api" in resp.text and "worker" in resp.text
    assert "3 pod" in resp.text


def test_staging_page_only_shows_staging_env_rows(client_factory, db_session):
    """Cache dung chung 1 bang cho ca staging/production - trang /staging KHONG duoc
    hien nham dong cua production."""
    make_cache_row(db_session, env="staging", project="core", application="api", replicas="1")
    make_cache_row(db_session, env="production", project="core", application="prod-only-app", replicas="1")

    sa = make_user(db_session, email="sa3@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)

    resp = client.get("/staging")

    assert "prod-only-app" not in resp.text


# ---------------------------------------------------------------------------
# POST /staging/sync - nut "Lam moi"
# ---------------------------------------------------------------------------


def test_sync_happy_path_populates_cache(client_factory, db_session, monkeypatch):
    monkeypatch.setattr(
        git_service, "list_staging_apps", lambda config: [("core", "api", "1"), ("core", "worker", "0")]
    )
    sa = make_user(db_session, email="syncer@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)

    resp = client.post("/staging/sync", headers={"Accept": "application/json"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    rows = db_session.query(EnvAppCache).filter_by(env="staging").all()
    assert {(r.project, r.application, r.replicas) for r in rows} == {("core", "api", "1"), ("core", "worker", "0")}


def test_sync_denied_for_user_without_permission(client_factory, db_session, monkeypatch):
    monkeypatch.setattr(git_service, "list_staging_apps", lambda config: [("core", "api", "1")])
    user = make_user(db_session, email="nosync@example.com", role=Role.USER, can_toggle_staging=False)
    client = client_factory(user=user)

    resp = client.post("/staging/sync", headers={"Accept": "application/json"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert db_session.query(EnvAppCache).count() == 0


def test_sync_reports_git_error_cleanly(client_factory, db_session, monkeypatch):
    def _raise(config):
        raise git_service.GitError("Repo URL chưa được cấu hình")

    monkeypatch.setattr(git_service, "list_staging_apps", _raise)
    sa = make_user(db_session, email="syncer2@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)

    resp = client.post("/staging/sync", headers={"Accept": "application/json"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "Đồng bộ thất bại" in body["message"]


# ---------------------------------------------------------------------------
# POST /staging/toggle
# ---------------------------------------------------------------------------


def test_toggle_happy_path_success_and_writes_through_cache(client_factory, db_session, monkeypatch):
    make_cache_row(db_session, env="staging", project="core", application="api", replicas="0")
    user = make_user(db_session, email="toggler@example.com", role=Role.USER, can_toggle_staging=True)
    client = client_factory(user=user)
    _stub_toggle_ok(monkeypatch)
    _stub_notify(monkeypatch)

    resp = client.post(
        "/staging/toggle",
        data={"project": "core", "application": "api", "enabled": "1", "replicas": "3"},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    row = db_session.query(EnvAppCache).filter_by(env="staging", project="core", application="api").first()
    assert row.replicas == "3"  # write-through: cache cap nhat ngay, khong can doi sync dinh ky


def test_toggle_denied_for_user_without_permission(client_factory, db_session, monkeypatch):
    user = make_user(db_session, email="noperm@example.com", role=Role.USER, can_toggle_staging=False)
    client = client_factory(user=user)
    _stub_toggle_ok(monkeypatch)

    resp = client.post(
        "/staging/toggle",
        data={"project": "core", "application": "api", "enabled": "1"},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_toggle_reports_git_level_not_found_when_app_missing_in_file(client_factory, db_session, monkeypatch):
    """Validate 'app co ton tai khong' hoan toan dua vao git_service (toggle_fn tra
    ok=False khi khong tim thay key trong file)."""
    user = make_user(db_session, email="toggler2@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=user)
    monkeypatch.setattr(
        git_service,
        "toggle_staging_app",
        lambda project, application, replicas, actor_email, config: git_service.ReplicasToggleResult(
            ok=False, message="Không tìm thấy ứng dụng nào khớp trong file"
        ),
    )
    _stub_notify(monkeypatch)

    resp = client.post(
        "/staging/toggle",
        data={"project": "not-a-group", "application": "not-an-app", "enabled": "1"},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "Không tìm thấy" in body["message"]


# ---------------------------------------------------------------------------
# POST /staging/bulk-toggle
# ---------------------------------------------------------------------------


def test_bulk_toggle_happy_path(client_factory, db_session, monkeypatch):
    make_cache_row(db_session, env="staging", project="core", application="api", replicas="0")
    make_cache_row(db_session, env="staging", project="core", application="worker", replicas="0")
    user = make_user(db_session, email="bulk@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=user)
    monkeypatch.setattr(
        git_service,
        "bulk_toggle_staging_apps",
        lambda apps, enabled, actor_email, config: git_service.ReplicasToggleResult(
            ok=True, message="ok", changed=tuple(apps)
        ),
    )
    _stub_notify(monkeypatch)

    resp = client.post(
        "/staging/bulk-toggle",
        data={"apps": ["core|api", "core|worker"], "enabled": "1"},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    rows = db_session.query(EnvAppCache).filter_by(env="staging").all()
    assert all(r.replicas == "1" for r in rows)


# ---------------------------------------------------------------------------
# POST /staging/turn-off-all - chi Admin/Super Admin
# ---------------------------------------------------------------------------


def test_turn_off_all_denied_for_plain_user_with_staging_flag(client_factory, db_session, monkeypatch):
    """User thuong (du duoc cap can_toggle_staging) KHONG duoc dung nut Tat tat ca."""
    user = make_user(db_session, email="user-with-flag@example.com", role=Role.USER, can_toggle_staging=True)
    client = client_factory(user=user)
    monkeypatch.setattr(
        git_service,
        "turn_off_all_staging",
        lambda actor_email, config: git_service.ReplicasToggleResult(ok=True, message="ok"),
    )

    resp = client.post("/staging/turn-off-all", headers={"Accept": "application/json"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_turn_off_all_denied_for_admin_without_staging_flag_granted(client_factory, db_session, monkeypatch):
    """Regression: Admin CHUA duoc Super Admin cap can_toggle_staging khong duoc goi thang
    POST /staging/turn-off-all du role la Admin - phai kiem CA can_access_staging LAN
    can_turn_off_all_staging, khong chi rieng 1 dieu kien."""
    admin = make_user(db_session, email="admin-noflag@example.com", role=Role.ADMIN, can_toggle_staging=False)
    client = client_factory(user=admin)
    monkeypatch.setattr(
        git_service,
        "turn_off_all_staging",
        lambda actor_email, config: git_service.ReplicasToggleResult(ok=True, message="ok"),
    )

    resp = client.post("/staging/turn-off-all", headers={"Accept": "application/json"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_turn_off_all_allowed_for_admin(client_factory, db_session, monkeypatch):
    make_cache_row(db_session, env="staging", project="core", application="api", replicas="3")
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN, can_toggle_staging=True)
    client = client_factory(user=admin)
    monkeypatch.setattr(
        git_service,
        "turn_off_all_staging",
        lambda actor_email, config: git_service.ReplicasToggleResult(
            ok=True, message="Đã tắt tất cả", changed=(("core", "api"),)
        ),
    )
    _stub_notify(monkeypatch)

    resp = client.post("/staging/turn-off-all", headers={"Accept": "application/json"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    row = db_session.query(EnvAppCache).filter_by(env="staging", project="core", application="api").first()
    assert row.replicas == "0"


# ---------------------------------------------------------------------------
# Quyen CHI XEM (can_view_staging, read-only) - vao xem duoc trang nhung KHONG sua duoc
# bat ky gi (sync/toggle/bulk-toggle/turn-off-all deu phai bi tu choi).
# ---------------------------------------------------------------------------


def test_staging_page_allowed_for_view_only_user(client_factory, db_session):
    """can_view_staging=True (KHONG can_toggle_staging) van vao xem duoc trang."""
    user = make_user(
        db_session, email="viewer@example.com", role=Role.USER, can_view_staging=True, can_toggle_staging=False
    )
    client = client_factory(user=user)

    resp = client.get("/staging")

    assert resp.status_code == 200


def test_staging_page_dims_mutating_controls_for_viewer(client_factory, db_session):
    """User CHI co can_view_staging (khong can_toggle_staging, khong
    can_toggle_restart_staging): form Bat/Tat va Restart PHAI VAN duoc render (khong an
    sau "—" nua) nhung nut ben trong PHAI bi disabled (lam mo), giong cach task_list.html
    lam mo Approve/Reject/Run khi thieu quyen. Sync/toolbar bulk van an hoan toan vi
    khong co quyen sua nao."""
    make_cache_row(db_session, env="staging", project="core", application="api", replicas="1")
    user = make_user(
        db_session, email="viewer2@example.com", role=Role.USER, can_view_staging=True, can_toggle_staging=False
    )
    client = client_factory(user=user)

    resp = client.get("/staging")

    assert resp.status_code == 200
    assert 'action="/staging/sync"' not in resp.text
    assert 'id="staging-bulk-toolbar"' not in resp.text

    toggle_form = _extract_form(resp.text, "/staging/toggle")
    assert "disabled" in toggle_form, "Form Bat/Tat phai render nhung nut ben trong bi lam mo"

    restart_form = _extract_form(resp.text, "/staging/restart")
    assert "disabled" in restart_form, "Form Restart phai render nhung nut ben trong bi lam mo"


@pytest.mark.parametrize(
    "path,data",
    [
        ("/staging/sync", {}),
        ("/staging/toggle", {"project": "core", "application": "api", "enabled": "1"}),
        ("/staging/bulk-toggle", {"apps": ["core|api"], "enabled": "1"}),
        ("/staging/turn-off-all", {}),
        ("/staging/restart", {"project": "core", "application": "api"}),
    ],
)
def test_view_only_user_cannot_mutate_staging(client_factory, db_session, monkeypatch, path, data):
    make_cache_row(db_session, env="staging", project="core", application="api", replicas="1")
    # Admin de thoa dieu kien role cua can_turn_off_all_staging - phai bi chan boi
    # can_write_staging du role la Admin va co can_view_staging.
    user = make_user(
        db_session,
        email="viewer3@example.com",
        role=Role.ADMIN,
        can_view_staging=True,
        can_toggle_staging=False,
    )
    client = client_factory(user=user)
    monkeypatch.setattr(
        git_service,
        "turn_off_all_staging",
        lambda actor_email, config: (_ for _ in ()).throw(AssertionError("khong duoc goi git that")),
    )

    resp = client.post(path, data=data, headers={"Accept": "application/json"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    row = db_session.query(EnvAppCache).filter_by(env="staging", project="core", application="api").first()
    assert row.replicas == "1", "Khong co bat ky thay doi nao duoc ghi cho user chi co quyen doc"


# ---------------------------------------------------------------------------
# Quyen Restart (can_toggle_restart_staging) TACH BIET hoan toan voi quyen sua Replicas
# (can_toggle_staging) tu 2026-08-18 - 1 user co the co quyen nay ma khong co quyen kia.
# ---------------------------------------------------------------------------


def test_replicas_only_user_cannot_restart(client_factory, db_session, monkeypatch):
    """User CHI duoc cap can_toggle_staging (sua Replicas) - KHONG duoc cap
    can_toggle_restart_staging - phai bi tu choi khi bam Restart, du toggle/sync van
    dung binh thuong."""
    make_cache_row(db_session, env="staging", project="core", application="api", replicas="1")
    user = make_user(
        db_session,
        email="replicas-only@example.com",
        role=Role.USER,
        can_toggle_staging=True,
        can_toggle_restart_staging=False,
    )
    client = client_factory(user=user)

    resp = client.post(
        "/staging/restart",
        data={"project": "core", "application": "api"},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_restart_only_user_can_restart_but_not_edit_replicas(client_factory, db_session, monkeypatch):
    """Nguoc lai: user CHI duoc cap can_toggle_restart_staging (Restart) - KHONG duoc cap
    can_toggle_staging - Restart phai THANH CONG, nhung toggle/sync/bulk-toggle van phai
    bi tu choi."""
    make_cache_row(db_session, env="staging", project="core", application="api", replicas="1")
    user = make_user(
        db_session,
        email="restart-only@example.com",
        role=Role.USER,
        can_toggle_staging=False,
        can_toggle_restart_staging=True,
    )
    client = client_factory(user=user)
    monkeypatch.setattr("app.services.argocd_service.settings.argocd_url_api", "")

    resp = client.post(
        "/staging/restart",
        data={"project": "core", "application": "api"},
        headers={"Accept": "application/json"},
    )
    # ArgoCD dang tat (argocd_url_api rong) nen restart_app tra ok=False voi ly do "ArgoCD
    # chua duoc cau hinh" - nhung DIEM CAN XAC NHAN la request KHONG bi chan boi permission
    # (message KHAC voi thong bao "Khong co quyen"). Xac nhan them qua RestartLog: neu bi
    # chan boi permission thi record_blocked_attempt() ghi log VOI message "Khong co
    # quyen..."; o day phai la 1 dong RestartLog toi tu restart_app() (di qua duoc
    # permission check) voi message ve ArgoCD chua cau hinh.
    assert resp.status_code == 200
    assert "Không có quyền" not in resp.json()["message"]
    log = db_session.query(RestartLog).filter_by(env="staging", project="core", application="api").first()
    assert log is not None
    assert log.ok is False
    assert "ArgoCD" in log.message

    toggle_resp = client.post(
        "/staging/toggle",
        data={"project": "core", "application": "api", "enabled": "1"},
        headers={"Accept": "application/json"},
    )
    assert toggle_resp.json()["ok"] is False
    row = db_session.query(EnvAppCache).filter_by(env="staging", project="core", application="api").first()
    assert row.replicas == "1"


def test_staging_page_enables_restart_button_but_dims_replicas_controls_for_restart_only_user(
    client_factory, db_session
):
    """User CHI co can_toggle_restart_staging (khong can_toggle_staging): nut Restart
    PHAI duoc BAT (khong disabled), nut Bat/Tat/Luu PHAI bi lam mo (disabled) - 2 quyen
    hoan toan doc lap tu 2026-08-18."""
    make_cache_row(db_session, env="staging", project="core", application="api", replicas="1")
    user = make_user(
        db_session,
        email="restart-only2@example.com",
        role=Role.USER,
        can_toggle_staging=False,
        can_toggle_restart_staging=True,
    )
    client = client_factory(user=user)

    resp = client.get("/staging")

    assert resp.status_code == 200
    assert 'action="/staging/sync"' not in resp.text

    toggle_form = _extract_form(resp.text, "/staging/toggle")
    assert "disabled" in toggle_form, "Khong co can_toggle_staging nen form Bat/Tat phai bi lam mo"

    restart_form = _extract_form(resp.text, "/staging/restart")
    assert "disabled" not in restart_form, "Co can_toggle_restart_staging nen nut Restart phai duoc BAT"


# ---------------------------------------------------------------------------
# POST /staging/bulk-restart - nut Restart trong toolbar bulk (tick nhieu app), dung
# can_restart_staging (KHONG dung can_write_staging), goi restart_service.bulk_restart_apps.
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
    make_cache_row(db_session, env="staging", project="core", application="api", replicas="1")
    make_cache_row(db_session, env="staging", project="core", application="worker", replicas="1")
    user = make_user(db_session, email="bulk-restart@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=user)

    resp = client.post(
        "/staging/bulk-restart",
        data={"apps": ["core|api", "core|worker"]},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "2" in body["message"]

    logs = db_session.query(RestartLog).filter_by(env="staging").all()
    assert len(logs) == 2
    assert all(log.ok for log in logs)


def test_bulk_restart_partial_failure_still_processes_all_and_reports_ok_false(client_factory, db_session, monkeypatch):
    """1 app KHONG co trong whitelist (EnvAppCache) - van phai bi tu choi (khong goi
    ArgoCD cho rieng app do), nhung app con lai VAN phai duoc restart thanh cong (khong
    dung som khi gap 1 loi) - ket qua tong the ok=False (co it nhat 1 that bai) nhung
    message phai neu ro chi tiet."""
    _enable_argocd(monkeypatch)
    _stub_deployment_tree(monkeypatch)
    make_cache_row(db_session, env="staging", project="core", application="api", replicas="1")
    # KHONG seed "core/not-exist" - se bi tu choi boi whitelist trong restart_app()
    user = make_user(db_session, email="bulk-restart-partial@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=user)

    resp = client.post(
        "/staging/bulk-restart",
        data={"apps": ["core|api", "core|not-exist"]},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "1/2" in body["message"]
    assert "not-exist" in body["message"]

    logs = db_session.query(RestartLog).filter_by(env="staging").order_by(RestartLog.id).all()
    assert len(logs) == 2, "Ca 2 app deu phai duoc thu (KHONG dung som khi gap 1 loi)"
    ok_by_app = {log.application: log.ok for log in logs}
    assert ok_by_app["api"] is True
    assert ok_by_app["not-exist"] is False


def test_bulk_restart_denied_without_restart_permission(client_factory, db_session, monkeypatch):
    _enable_argocd(monkeypatch)
    called = {"post": False}
    monkeypatch.setattr(argocd_service.httpx, "get", lambda *a, **k: _FakeArgoResponse({}))
    monkeypatch.setattr(argocd_service.httpx, "post", lambda *a, **k: called.__setitem__("post", True) or _FakeArgoResponse({}))
    make_cache_row(db_session, env="staging", project="core", application="api", replicas="1")
    # Co quyen sua Replicas nhung KHONG co quyen Restart - phai bi tu choi.
    user = make_user(
        db_session,
        email="bulk-restart-denied@example.com",
        role=Role.USER,
        can_toggle_staging=True,
        can_toggle_restart_staging=False,
    )
    client = client_factory(user=user)

    resp = client.post(
        "/staging/bulk-restart",
        data={"apps": ["core|api"]},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert called["post"] is False, "Khong duoc goi ArgoCD khi khong co quyen Restart"


def test_bulk_restart_no_matching_apps_after_parsing_is_rejected(client_factory, db_session, monkeypatch):
    """apps chi chua item SAI DINH DANG (khong co "|") - bi loc bo het trong vong for cua
    router, ket qua la list rong truyen vao bulk_restart_apps() -> tu choi voi thong bao
    ro rang thay vi crash hay silently thanh cong. (Gui `apps=[]` that su bi FastAPI tra
    422 truoc ca khi vao duoc router - hanh vi co san giong het /staging/bulk-toggle,
    khong phai code moi cua tinh nang nay nen khong test lai o day.)"""
    _enable_argocd(monkeypatch)
    user = make_user(db_session, email="bulk-restart-empty@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=user)

    resp = client.post(
        "/staging/bulk-restart",
        data={"apps": ["missing-separator"]},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["message"] == "Chưa chọn ứng dụng nào"
