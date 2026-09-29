"""[TEST] Kiểm thử happy path tính năng "Restart" (POST /staging/restart, POST
/production/restart) vừa được [dev] triển khai - xem app/services/restart_service.py +
app/services/argocd_service.py::restart_workload. Mock ArgoCD qua httpx.get/httpx.post
(argocd_service.py) - KHÔNG gọi ArgoCD thật, theo đúng pattern tests/test_argocd_service.py.

Phạm vi: CHỈ 3 AC luồng chính do Leader giao (AC1/AC2/AC3), không test edge case."""

import json

from app.models import EnvAppCache, RestartLog, Role
from app.services import argocd_service
from app.services.app_setting_service import update_app_setting
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


# ---------------------------------------------------------------------------
# AC1: POST /production/restart - resource-tree co 1 node Deployment -> goi API Resource
# Actions cua ArgoCD (action "restart") -> ok=True, dung 1 RestartLog (ok=True, dung
# project/application/env)
# ---------------------------------------------------------------------------


def test_ac1_production_restart_happy_path_deployment(client_factory, db_session, monkeypatch):
    _enable_argocd(monkeypatch)
    make_cache_row(db_session, env="production", project="core", application="api")
    user = make_user(db_session, email="prod-restart@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=user)

    tree = {
        "nodes": [
            {"kind": "Deployment", "name": "core-api", "namespace": "prod-ns", "group": "apps", "version": "v1"}
        ]
    }
    action_calls = []
    monkeypatch.setattr(argocd_service.httpx, "get", lambda *a, **k: FakeResponse(tree))

    def _fake_post(url, params=None, headers=None, content=None, timeout=None):
        action_calls.append({"url": url, "params": params, "content": content})
        return FakeResponse({})

    monkeypatch.setattr(argocd_service.httpx, "post", _fake_post)

    resp = client.post(
        "/production/restart",
        data={"project": "core", "application": "api"},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True, body

    assert len(action_calls) == 1, "Phai goi API Resource Actions cua ArgoCD dung 1 lan"
    assert action_calls[0]["url"].endswith("/resource/actions")
    # content la JSON string literal boc quanh ten action (body schema ArgoCD la
    # "type": "string") - vd '"restart"'.
    action_name = json.loads(action_calls[0]["content"])
    assert action_name == "restart"
    assert action_calls[0]["params"]["kind"] == "Deployment"

    logs = db_session.query(RestartLog).all()
    assert len(logs) == 1, "Phai ghi dung 1 dong RestartLog"
    log = logs[0]
    assert log.ok is True
    assert log.project == "core"
    assert log.application == "api"
    assert log.env == "production"
    assert log.user_email == "prod-restart@example.com"
    assert log.workload_kind == "Deployment"


# ---------------------------------------------------------------------------
# AC2: POST /staging/restart - resource-tree co 1 node StatefulSet -> tu phat hien dung
# StatefulSet (workload_kind trong RestartLog = "StatefulSet"), ok=True
# ---------------------------------------------------------------------------


def test_ac2_staging_restart_happy_path_statefulset_autodetected(client_factory, db_session, monkeypatch):
    _enable_argocd(monkeypatch)
    make_cache_row(db_session, env="staging", project="core", application="worker")
    user = make_user(db_session, email="staging-restart@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=user)

    tree = {
        "nodes": [
            # Node khong phai workload dung truoc trong list - de xac nhan restart_workload
            # thuc su LOC theo kind (Deployment/StatefulSet), khong chi nhat "node dau tien"
            # bat ky trong list.
            {"kind": "Service", "name": "core-worker-svc", "namespace": "staging-ns", "group": "", "version": "v1"},
            {"kind": "StatefulSet", "name": "core-worker", "namespace": "staging-ns", "group": "apps", "version": "v1"},
        ]
    }
    action_calls = []
    monkeypatch.setattr(argocd_service.httpx, "get", lambda *a, **k: FakeResponse(tree))

    def _fake_post(url, params=None, headers=None, content=None, timeout=None):
        action_calls.append({"url": url, "params": params, "content": content})
        return FakeResponse({})

    monkeypatch.setattr(argocd_service.httpx, "post", _fake_post)

    resp = client.post(
        "/staging/restart",
        data={"project": "core", "application": "worker"},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True, body

    assert len(action_calls) == 1
    assert action_calls[0]["params"]["kind"] == "StatefulSet"

    logs = db_session.query(RestartLog).all()
    assert len(logs) == 1
    log = logs[0]
    assert log.ok is True
    assert log.env == "staging"
    assert log.workload_kind == "StatefulSet"
    assert log.workload_name == "core-worker"


# ---------------------------------------------------------------------------
# AC3: /staging va /production render duoc, moi dong app co nut Restart, cot "Trang thai"
# (badge Bat/Tat) KHONG bi thay doi y nghia boi tinh nang moi
# ---------------------------------------------------------------------------


def test_ac3_staging_and_production_pages_render_restart_button_and_status_badge_unchanged(
    client_factory, db_session
):
    make_cache_row(db_session, env="staging", project="core", application="api", replicas="3")
    make_cache_row(db_session, env="production", project="core", application="api", replicas="0")
    sa = make_user(db_session, email="sa-ac3@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)

    staging_resp = client.get("/staging")
    production_resp = client.get("/production")

    assert staging_resp.status_code == 200
    assert production_resp.status_code == 200

    # Nut Restart co mat, tro dung action, mang du lieu project/application dung dong
    assert 'action="/staging/restart"' in staging_resp.text
    assert 'action="/production/restart"' in production_resp.text

    # Badge "Trang thai" van hien dung y nghia cu: ON kem so pod (staging replicas=3),
    # OFF (production replicas=0) - tinh nang Restart KHONG duoc doi logic nay
    assert "ON (3 pod)" in staging_resp.text
    assert "OFF" in production_resp.text


# ---------------------------------------------------------------------------
# [sec] AC4: Restart app KHONG co trong EnvAppCache (whitelist) -> bi tu choi, KHONG goi
# ArgoCD (vs lo hong #1: truoc day chi kiem co TOAN CUC can_access_staging/production,
# khong doi chieu voi danh sach app THAT SU duoc hien thi tren trang)
# ---------------------------------------------------------------------------


def test_ac4_restart_app_not_in_whitelist_is_rejected_and_argocd_not_called(client_factory, db_session, monkeypatch):
    _enable_argocd(monkeypatch)
    # Chi seed "core/api" vao whitelist production - KHONG seed "other/app"
    make_cache_row(db_session, env="production", project="core", application="api")
    user = make_user(db_session, email="prod-whitelist@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=user)

    called = {"get": False, "post": False}
    monkeypatch.setattr(argocd_service.httpx, "get", lambda *a, **k: called.__setitem__("get", True) or FakeResponse({}))
    monkeypatch.setattr(argocd_service.httpx, "post", lambda *a, **k: called.__setitem__("post", True) or FakeResponse({}))

    resp = client.post(
        "/production/restart",
        data={"project": "other", "application": "app"},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False, body

    assert called["get"] is False, "Khong duoc goi ArgoCD GET resource-tree khi app khong trong whitelist"
    assert called["post"] is False, "Khong duoc goi ArgoCD Resource Actions khi app khong trong whitelist"

    logs = db_session.query(RestartLog).all()
    assert len(logs) == 1, "Van phai ghi 1 dong RestartLog (ok=False) cho lan bi chan"
    log = logs[0]
    assert log.ok is False
    assert log.project == "other"
    assert log.application == "app"
    assert log.env == "production"


# ---------------------------------------------------------------------------
# AC5 [regression - incident 2026-08-18, lan 1]: ArgoCD RunResourceAction RPC anh xa HTTP
# POST (khong phai PATCH) va body schema la "type": "string" (xac nhan qua
# server/application/application.proto + assets/swagger.json chinh thuc cua
# argoproj/argo-cd). Test nay KHONG mock argocd_service.httpx.post o muc "chap nhan bat ky
# call nao" nhu cac test khac, ma pin CHINH XAC HTTP method + duong dan + body encoding de
# khong regress lai loi cu neu sau nay co ai doi nham method/body ma khong biet ArgoCD
# that se tu choi (405).
# ---------------------------------------------------------------------------


def test_ac5_restart_calls_argocd_resource_actions_with_post_method_and_string_body(
    client_factory, db_session, monkeypatch
):
    _enable_argocd(monkeypatch)
    make_cache_row(db_session, env="production", project="core", application="api")
    user = make_user(db_session, email="method-check@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=user)

    tree = {
        "nodes": [
            {"kind": "Deployment", "name": "core-api", "namespace": "prod-ns", "group": "apps", "version": "v1"}
        ]
    }
    monkeypatch.setattr(argocd_service.httpx, "get", lambda *a, **k: FakeResponse(tree))

    def _patch_verb_must_not_be_called(*a, **k):
        raise AssertionError(
            "ArgoCD RunResourceAction anh xa HTTP POST, khong phai PATCH - goi httpx.patch() "
            "se bi ArgoCD server that tra ve 405 Method Not Allowed (xem incident 2026-08-18)"
        )

    post_calls = []

    def _fake_post(url, params=None, headers=None, content=None, timeout=None):
        post_calls.append({"url": url, "params": params, "content": content})
        return FakeResponse({})

    monkeypatch.setattr(argocd_service.httpx, "patch", _patch_verb_must_not_be_called)
    monkeypatch.setattr(argocd_service.httpx, "post", _fake_post)

    resp = client.post(
        "/production/restart",
        data={"project": "core", "application": "api"},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    assert len(post_calls) == 1, "Phai goi httpx.post() dung 1 lan (khong phai httpx.patch())"
    # Resource Actions API (RunResourceAction), KHONG phai PatchResource - la endpoint dung
    # RBAC action string ("action/<group>/<kind>/<action>") khop voi quyen ArgoCD thuc te
    # (vd "action/apps/Deployment/restart") ma ban dau code goi nham endpoint PatchResource
    # (can quyen "update" khac, gay 403 tren production - xem incident 2026-08-18 lan 2).
    assert post_calls[0]["url"].endswith("/resource/actions")
    assert "patchType" not in (post_calls[0]["params"] or {}), (
        "Resource Actions API khong dung tham so patchType (dac trung cua PatchResource cu)"
    )

    # Body schema cua ArgoCD la "type": "string" -> raw HTTP body phai la 1 JSON string
    # literal chua dung TEN ACTION "restart" (khong phai JSON object patch nhu truoc).
    raw_content = post_calls[0]["content"]
    assert raw_content == '"restart"', f"Body phai la JSON string '\"restart\"', nhan duoc: {raw_content!r}"


# ---------------------------------------------------------------------------
# AC6 [regression - incident 2026-08-18, lan 3]: Staging va Production PHAI dung 2 hau to
# ArgoCD Application KHAC NHAU (AppSetting.argocd_application_postfix_staging /
# ..._production) - truoc ban va, ca 2 nut Restart dung CHUNG 1 postfix toan cuc nen deu
# tac dong vao CUNG 1 Application ArgoCD (vd ca 2 nut deu restart nham app hau to
# "-sandbox" du dang bam nut o trang Staging).
# ---------------------------------------------------------------------------


def test_ac6_staging_and_production_restart_use_different_argocd_postfix(client_factory, db_session, monkeypatch):
    _enable_argocd(monkeypatch)
    update_app_setting(
        db_session,
        argocd_application_postfix_staging="-dev",
        argocd_application_postfix_production="-sandbox",
    )
    make_cache_row(db_session, env="staging", project="core", application="api")
    make_cache_row(db_session, env="production", project="core", application="api")
    user = make_user(db_session, email="postfix-check@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=user)

    tree = {
        "nodes": [
            {"kind": "Deployment", "name": "core-api", "namespace": "ns", "group": "apps", "version": "v1"}
        ]
    }
    requested_urls = []
    monkeypatch.setattr(
        argocd_service.httpx,
        "get",
        lambda url, *a, **k: requested_urls.append(url) or FakeResponse(tree),
    )
    monkeypatch.setattr(argocd_service.httpx, "post", lambda *a, **k: FakeResponse({}))

    staging_resp = client.post(
        "/staging/restart",
        data={"project": "core", "application": "api"},
        headers={"Accept": "application/json"},
    )
    production_resp = client.post(
        "/production/restart",
        data={"project": "core", "application": "api"},
        headers={"Accept": "application/json"},
    )

    assert staging_resp.json()["ok"] is True
    assert production_resp.json()["ok"] is True
    assert len(requested_urls) == 2

    assert "core-api-dev" in requested_urls[0], (
        f"Restart Staging phai goi ArgoCD Application co hau to '-dev', nhan duoc URL: {requested_urls[0]}"
    )
    assert "core-api-sandbox" in requested_urls[1], (
        f"Restart Production phai goi ArgoCD Application co hau to '-sandbox', nhan duoc URL: {requested_urls[1]}"
    )
