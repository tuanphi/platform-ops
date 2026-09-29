"""[TEST] Kiểm thử LẠI tính năng "Restart" (POST /staging/restart, POST /production/restart)
SAU KHI [dev] vá 5 lỗ hổng bảo mật ([sec] audit) trên app/services/restart_service.py +
app/services/argocd_service.py + app/routers/{staging,production}_router.py.

File này CHỈ bổ sung test cho các chốt chặn MỚI (whitelist đã có sẵn AC4 trong
tests/test_restart_feature.py - không lặp lại): rate-limit cooldown, audit log cho lần bị
chặn (CSRF/permission), message riêng theo env, và 1 rủi ro về ĐỘ TIN CẬY của test suite
(state rate-limit rò rỉ giữa các test vì conftest.py không reset). KHÔNG sửa app/, chỉ thêm
test mới, dùng chung fixture/pattern với tests/test_restart_feature.py và
tests/test_run_rollback_rate_limit.py (mock ArgoCD qua monkeypatch httpx.get/httpx.post,
KHÔNG gọi ArgoCD thật)."""

import time

from app.models import EnvAppCache, RestartLog, Role
from app.services import argocd_service, restart_service
from app.services.app_setting_service import get_app_setting
from tests.conftest import make_user


class FakeResponse:
    def __init__(self, json_data=None):
        self._json_data = json_data or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._json_data


def _enable_argocd(monkeypatch):
    monkeypatch.setattr(argocd_service.settings, "argocd_url_api", "https://argocd.example.com/api/v1/applications")
    monkeypatch.setattr(argocd_service.settings, "argocd_token", "fake-token")


def make_cache_row(db_session, env, project="core", application="api", replicas="1"):
    row = EnvAppCache(env=env, project=project, application=application, replicas=replicas)
    db_session.add(row)
    db_session.commit()
    return row


def _stub_deployment_tree(monkeypatch, patch_calls):
    tree = {"nodes": [{"kind": "Deployment", "name": "x", "namespace": "ns", "group": "apps", "version": "v1"}]}
    monkeypatch.setattr(argocd_service.httpx, "get", lambda *a, **k: FakeResponse(tree))

    def _fake_patch(url, params=None, headers=None, content=None, timeout=None):
        patch_calls.append(1)
        return FakeResponse({})

    monkeypatch.setattr(argocd_service.httpx, "post", _fake_patch)


# ---------------------------------------------------------------------------
# B1. Rate limit: 2 lan Restart lien tiep CUNG (env, project, application) -> lan 2 bi chan
# voi message cooldown, ArgoCD chi duoc goi 1 lan (patch chi 1 lan).
# ---------------------------------------------------------------------------


def test_rate_limit_second_restart_same_app_back_to_back_is_blocked_argocd_called_once(
    client_factory, db_session, monkeypatch
):
    _enable_argocd(monkeypatch)
    make_cache_row(db_session, env="production", project="rl-proj", application="rl-app")
    user = make_user(db_session, email="rl-restart@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=user)

    patch_calls = []
    _stub_deployment_tree(monkeypatch, patch_calls)

    resp1 = client.post(
        "/production/restart",
        data={"project": "rl-proj", "application": "rl-app"},
        headers={"Accept": "application/json"},
    )
    assert resp1.status_code == 200 and resp1.json()["ok"] is True

    resp2 = client.post(
        "/production/restart",
        data={"project": "rl-proj", "application": "rl-app"},
        headers={"Accept": "application/json"},
    )
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["ok"] is False
    # Cooldown gio la AppSetting.restart_cooldown_seconds (sua duoc qua Settings, mac dinh
    # 60) thay vi hang so cung trong code - tinh message ky vong tu gia tri THAT SU dang
    # cau hinh trong DB test (mac dinh cua AppSetting), khong hardcode so giay.
    expected_cooldown = get_app_setting(db_session).restart_cooldown_seconds
    assert body2["message"] == restart_service._restart_rate_limit_message(expected_cooldown), body2

    assert len(patch_calls) == 1, "ArgoCD PATCH chi duoc goi 1 lan (lan 2 phai bi chan TRUOC khi cham ArgoCD)"

    logs = db_session.query(RestartLog).order_by(RestartLog.id).all()
    assert len(logs) == 2, "Ca 2 lan (thanh cong + bi chan) deu phai duoc ghi RestartLog"
    assert logs[0].ok is True
    assert logs[1].ok is False
    assert logs[1].message == restart_service._restart_rate_limit_message(expected_cooldown)


def test_rate_limit_different_app_same_request_is_not_blocked_by_other_apps_cooldown(
    client_factory, db_session, monkeypatch
):
    """Cooldown khoa theo (env, project, application) - Restart app KHAC ngay sau do
    (cung env) KHONG duoc bi chan oan boi cooldown cua app truoc."""
    _enable_argocd(monkeypatch)
    make_cache_row(db_session, env="staging", project="p1", application="app-a")
    make_cache_row(db_session, env="staging", project="p1", application="app-b")
    user = make_user(db_session, email="rl-diffapp@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=user)

    patch_calls = []
    _stub_deployment_tree(monkeypatch, patch_calls)

    resp1 = client.post(
        "/staging/restart", data={"project": "p1", "application": "app-a"}, headers={"Accept": "application/json"}
    )
    assert resp1.status_code == 200 and resp1.json()["ok"] is True

    resp2 = client.post(
        "/staging/restart", data={"project": "p1", "application": "app-b"}, headers={"Accept": "application/json"}
    )
    assert resp2.status_code == 200
    assert resp2.json()["ok"] is True, "App khac (cung env/project khac application) khong duoc bi chan boi cooldown cua app-a"
    assert len(patch_calls) == 2


def test_rate_limit_succeeds_again_after_cooldown_elapses(client_factory, db_session, monkeypatch):
    """Sau khi cooldown (AppSetting.restart_cooldown_seconds) troi qua (mo phong bang cach
    lui moc thoi gian da ghi nhan trong dict module-level, giong pattern
    test_run_rollback_rate_limit.py), Restart lai phai thanh cong binh thuong."""
    _enable_argocd(monkeypatch)
    make_cache_row(db_session, env="production", project="elapsed-proj", application="elapsed-app")
    user = make_user(db_session, email="rl-elapsed-restart@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=user)

    patch_calls = []
    _stub_deployment_tree(monkeypatch, patch_calls)

    resp1 = client.post(
        "/production/restart",
        data={"project": "elapsed-proj", "application": "elapsed-app"},
        headers={"Accept": "application/json"},
    )
    assert resp1.status_code == 200 and resp1.json()["ok"] is True

    cooldown = get_app_setting(db_session).restart_cooldown_seconds
    key = ("production", "elapsed-proj", "elapsed-app")
    restart_service._restart_last_call[key] = time.monotonic() - (cooldown + 1)

    resp2 = client.post(
        "/production/restart",
        data={"project": "elapsed-proj", "application": "elapsed-app"},
        headers={"Accept": "application/json"},
    )
    assert resp2.status_code == 200
    assert resp2.json()["ok"] is True, "Sau khi cooldown troi qua, Restart lai phai thanh cong (khong con bi chan)"
    assert len(patch_calls) == 2


# ---------------------------------------------------------------------------
# B2. Audit log cho lan bi chan TRUOC khi vao restart_app() (CSRF sai, thieu quyen) -
# record_blocked_attempt() phai ghi RestartLog(ok=False) va KHONG duoc goi ArgoCD.
# ---------------------------------------------------------------------------


def test_blocked_by_invalid_csrf_is_audit_logged_and_argocd_not_called(client_factory, db_session, monkeypatch):
    _enable_argocd(monkeypatch)
    make_cache_row(db_session, env="staging", project="csrf-proj", application="csrf-app")
    user = make_user(db_session, email="csrf-restart@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=user)

    called = {"get": False, "patch": False}
    monkeypatch.setattr(argocd_service.httpx, "get", lambda *a, **k: called.__setitem__("get", True) or FakeResponse({}))
    monkeypatch.setattr(argocd_service.httpx, "post", lambda *a, **k: called.__setitem__("patch", True) or FakeResponse({}))

    resp = client.post(
        "/staging/restart",
        data={"project": "csrf-proj", "application": "csrf-app", "csrf_token": "obviously-wrong-token"},
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "hết hạn" in body["message"] or "hợp lệ" in body["message"], body

    assert called["get"] is False and called["patch"] is False, "CSRF sai phai chan TRUOC khi goi ArgoCD"

    logs = db_session.query(RestartLog).all()
    assert len(logs) == 1, "Lan bi chan vi CSRF sai van phai duoc ghi 1 dong RestartLog (audit)"
    assert logs[0].ok is False
    assert logs[0].project == "csrf-proj"
    assert logs[0].application == "csrf-app"
    assert logs[0].env == "staging"
    assert "CSRF" in logs[0].message


def test_blocked_by_missing_permission_is_audit_logged_with_restart_specific_message(
    client_factory, db_session, monkeypatch
):
    _enable_argocd(monkeypatch)
    make_cache_row(db_session, env="production", project="perm-proj", application="perm-app")
    plain_user = make_user(db_session, email="perm-restart@example.com", role=Role.USER)
    client = client_factory(user=plain_user)

    called = {"get": False, "patch": False}
    monkeypatch.setattr(argocd_service.httpx, "get", lambda *a, **k: called.__setitem__("get", True) or FakeResponse({}))
    monkeypatch.setattr(argocd_service.httpx, "post", lambda *a, **k: called.__setitem__("patch", True) or FakeResponse({}))

    resp = client.post(
        "/production/restart",
        data={"project": "perm-proj", "application": "perm-app"},
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["message"] == "Bạn không có quyền dùng tính năng Restart Production", body

    assert called["get"] is False and called["patch"] is False, "Thieu quyen phai chan TRUOC khi goi ArgoCD"

    logs = db_session.query(RestartLog).all()
    assert len(logs) == 1, "Lan bi chan vi thieu quyen van phai duoc ghi 1 dong RestartLog (audit)"
    assert logs[0].ok is False
    assert "Restart Production" in logs[0].message


def test_permission_denied_message_is_staging_specific_not_reused_from_production(
    client_factory, db_session
):
    """Message tu choi quyen phai rieng cho tung menu (Staging/Production) - khong bi loi
    copy-paste dung chung 1 chuoi giua 2 router."""
    plain_user = make_user(db_session, email="perm-staging@example.com", role=Role.USER)
    client = client_factory(user=plain_user)

    resp = client.post(
        "/staging/restart",
        data={"project": "any", "application": "any"},
        headers={"Accept": "application/json"},
    )
    body = resp.json()
    assert body["ok"] is False
    assert body["message"] == "Bạn không có quyền dùng tính năng Restart Staging", body


# ---------------------------------------------------------------------------
# B3. Regex validate ten project/application (lop phong thu thu 2 cung voi urllib.parse.quote)
# ---------------------------------------------------------------------------


def test_project_name_with_path_traversal_characters_is_rejected_before_whitelist_query(
    client_factory, db_session, monkeypatch
):
    _enable_argocd(monkeypatch)
    user = make_user(db_session, email="regex-restart@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=user)

    called = {"get": False, "patch": False}
    monkeypatch.setattr(argocd_service.httpx, "get", lambda *a, **k: called.__setitem__("get", True) or FakeResponse({}))
    monkeypatch.setattr(argocd_service.httpx, "post", lambda *a, **k: called.__setitem__("patch", True) or FakeResponse({}))

    resp = client.post(
        "/production/restart",
        data={"project": "../secret", "application": "app"},
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert called["get"] is False and called["patch"] is False

    logs = db_session.query(RestartLog).all()
    assert len(logs) == 1
    assert logs[0].ok is False
    assert logs[0].message == "Tên project/application không hợp lệ"


# ---------------------------------------------------------------------------
# B4. [DA VA - xem conftest.py::client_factory] conftest.py::client_factory gio reset CA
# actions_router_module._run_rollback_last_call LAN restart_service._restart_last_call
# truoc MOI test (cung pattern, dict rate-limit cap MODULE khoa theo (env_label, project,
# application)). Test nay xac nhan dict luon rong khi 1 test moi bat dau (khong con state
# sot lai tu test khac chay truoc do trong cung tien trinh pytest) va 1 request Restart
# hoan toan moi phai thanh cong (ok=True), khong bi chan oan boi cooldown.
# ---------------------------------------------------------------------------


def test_restart_rate_limit_state_is_reset_between_independent_tests(
    client_factory, db_session, monkeypatch
):
    _enable_argocd(monkeypatch)
    make_cache_row(db_session, env="staging", project="leak-check", application="svc")
    user = make_user(db_session, email="leak-check@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=user)

    key = ("staging", "leak-check", "svc")
    assert key not in restart_service._restart_last_call, (
        "conftest.py::client_factory phai reset restart_service._restart_last_call ve rong "
        "truoc MOI test - neu key nay da co san o day, nghia la buoc reset bi thieu/sai va "
        "state rate-limit dang ro ri giua cac test doc lap."
    )

    patch_calls = []
    _stub_deployment_tree(monkeypatch, patch_calls)

    resp = client.post(
        "/staging/restart", data={"project": "leak-check", "application": "svc"}, headers={"Accept": "application/json"}
    )
    body = resp.json()
    assert body["ok"] is True, (
        "Request Restart hoan toan moi (client/user/db rieng cua RIENG test nay) phai thanh "
        "cong - neu bi chan voi message cooldown, nghia la state rate-limit da ro ri tu 1 "
        "test khac dung chung (env, project, application) vi conftest.py khong reset dung "
        "restart_service._restart_last_call giua cac test."
    )
    assert len(patch_calls) == 1
