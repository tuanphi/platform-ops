"""[TEST] Kiểm thử LẠI tính năng "Restart" SAU bản vá vòng 2 ([sec] tái thẩm định phát
hiện 1 MEDIUM - CWE-770 log/DB flooding qua limiter theo env/project/application cũ nhưng
CHƯA khoá theo user.id). File này CHỈ bổ sung test cho chốt chặn MỚI
(restart_service.check_restart_attempt_rate_limit, khoá theo user.id, 10 lần/60s, gọi TRƯỚC
CẢ check_csrf_token) - whitelist/regex/cooldown-per-app/audit-log đã có sẵn trong
tests/test_restart_feature.py và tests/test_restart_feature_patch_verification.py, KHÔNG lặp
lại ở đây. KHÔNG sửa app/, chỉ thêm test mới, dùng chung fixture/pattern với 2 file trên
(mock ArgoCD qua monkeypatch httpx.get/httpx.post, KHÔNG gọi ArgoCD thật)."""

from app.models import EnvAppCache, RestartLog, Role
from app.services import argocd_service, restart_service
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
# B1. 11 lan Restart HOP LE (10 app khac nhau tranh dinh cooldown-per-app 30s cua limiter
# cu) boi CUNG 1 user trong < 60s -> 10 lan dau thanh cong, lan thu 11 bi chan 429 va
# KHONG phat sinh them row RestartLog (diem mau chot cua ban va).
# ---------------------------------------------------------------------------


def test_11th_call_same_user_is_429_and_writes_no_additional_restart_log(client_factory, db_session, monkeypatch):
    _enable_argocd(monkeypatch)
    for i in range(11):
        make_cache_row(db_session, env="production", project="rl2", application=f"app-{i}")
    user = make_user(db_session, email="rl2-user@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=user)

    patch_calls = []
    _stub_deployment_tree(monkeypatch, patch_calls)

    for i in range(10):
        resp = client.post(
            "/production/restart",
            data={"project": "rl2", "application": f"app-{i}"},
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True, f"Lan {i + 1} (<=10) phai thanh cong"

    logs_before = db_session.query(RestartLog).count()
    assert logs_before == 10, "10 lan hop le dau tien phai ghi dung 10 dong RestartLog"

    resp11 = client.post(
        "/production/restart",
        data={"project": "rl2", "application": "app-10"},
        headers={"Accept": "application/json"},
    )
    assert resp11.status_code == 429, "Lan goi thu 11 trong cua so 60s phai bi chan 429"
    body11 = resp11.json()
    assert body11["ok"] is False
    assert body11["message"] == restart_service.RESTART_ATTEMPT_RATE_LIMIT_MESSAGE, body11

    logs_after = db_session.query(RestartLog).count()
    assert logs_after == logs_before, (
        "Lan bi chan boi limiter theo user.id KHONG duoc ghi them RestartLog nao (day la "
        "diem mau chot cua ban va CWE-770 - neu con tang, nghia la limiter dang duoc dat SAU "
        "record_blocked_attempt() hoac khong that su chan truoc khi ghi log)"
    )
    assert len(patch_calls) == 10, "ArgoCD KHONG duoc goi them lan nao cho request bi 429"


# ---------------------------------------------------------------------------
# B2. Limiter theo user.id dat TRUOC CA check_csrf_token(): spam > 10 lan voi
# csrf_token SAI boi CUNG 1 user -> tu lan thu 11, phai la 429 (khong phai thong bao CSRF)
# va so dong RestartLog dung lai (truoc ban va se tang vo han).
# ---------------------------------------------------------------------------


def test_rate_limit_before_csrf_check_wrong_csrf_stops_log_growth_after_10th_call(
    client_factory, db_session, monkeypatch
):
    _enable_argocd(monkeypatch)
    make_cache_row(db_session, env="staging", project="csrf-flood", application="app")
    user = make_user(db_session, email="csrf-flood-user@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=user)

    called = {"get": False, "patch": False}
    monkeypatch.setattr(argocd_service.httpx, "get", lambda *a, **k: called.__setitem__("get", True) or FakeResponse({}))
    monkeypatch.setattr(argocd_service.httpx, "post", lambda *a, **k: called.__setitem__("patch", True) or FakeResponse({}))

    for i in range(10):
        resp = client.post(
            "/staging/restart",
            data={"project": "csrf-flood", "application": "app", "csrf_token": "always-wrong-token"},
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200, f"Lan {i + 1} (<=10, csrf sai) van phai qua duoc limiter -> 200 (ok=False do CSRF)"
        body = resp.json()
        assert body["ok"] is False
        assert "CSRF" in body["message"] or "hết hạn" in body["message"] or "hợp lệ" in body["message"], body

    logs_at_10 = db_session.query(RestartLog).count()
    assert logs_at_10 == 10, "10 lan CSRF sai dau tien deu phai duoc audit-log (record_blocked_attempt)"

    for extra in range(5):
        resp = client.post(
            "/staging/restart",
            data={"project": "csrf-flood", "application": "app", "csrf_token": "always-wrong-token"},
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 429, f"Lan {11 + extra} (spam CSRF sai) phai bi 429 boi limiter theo user, KHONG con toi duoc buoc check CSRF"
        body = resp.json()
        assert body["message"] == restart_service.RESTART_ATTEMPT_RATE_LIMIT_MESSAGE, body

    logs_final = db_session.query(RestartLog).count()
    assert logs_final == logs_at_10, (
        "Truoc ban va vong 2, spam CSRF sai se lam RestartLog tang VO HAN (CWE-770) - sau "
        "ban va, so dong phai DUNG LAI o 10 du gui them nhieu request nua"
    )
    assert called["get"] is False and called["patch"] is False, "ArgoCD khong bao gio duoc goi trong toan bo kich ban nay"


# ---------------------------------------------------------------------------
# B3. Limiter tach theo user.id: user A bi chan (da dung het 10 luot) KHONG lam user B
# (khac user_id) bi anh huong - van goi Restart binh thuong duoc.
# ---------------------------------------------------------------------------


def test_rate_limit_is_isolated_per_user_id(client_factory, db_session, monkeypatch):
    _enable_argocd(monkeypatch)
    for i in range(10):
        make_cache_row(db_session, env="production", project="iso", application=f"a-{i}")
    make_cache_row(db_session, env="production", project="iso", application="b-app")

    user_a = make_user(db_session, email="user-a@example.com", role=Role.SUPER_ADMIN)
    user_b = make_user(db_session, email="user-b@example.com", role=Role.SUPER_ADMIN)

    client_a = client_factory(user=user_a)
    patch_calls = []
    _stub_deployment_tree(monkeypatch, patch_calls)

    for i in range(10):
        resp = client_a.post(
            "/production/restart",
            data={"project": "iso", "application": f"a-{i}"},
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200 and resp.json()["ok"] is True

    resp_a_11 = client_a.post(
        "/production/restart",
        data={"project": "iso", "application": "a-9"},
        headers={"Accept": "application/json"},
    )
    assert resp_a_11.status_code == 429, "User A phai da het luot (429) sau 10 lan"

    client_b = client_factory(user=user_b)
    resp_b = client_b.post(
        "/production/restart",
        data={"project": "iso", "application": "b-app"},
        headers={"Accept": "application/json"},
    )
    assert resp_b.status_code == 200, "User B (user_id khac) KHONG duoc bi anh huong boi viec User A da het luot"
    assert resp_b.json()["ok"] is True, resp_b.json()


# ---------------------------------------------------------------------------
# B4. Duoi nguong (<=10 lan/60s) KHONG duoc chan oan thao tac hop le - restart lan luot
# nhieu app khac nhau (kich ban that: sau 1 dot deploy nhieu service).
# ---------------------------------------------------------------------------


def test_rate_limit_does_not_block_valid_sequential_restarts_under_threshold(
    client_factory, db_session, monkeypatch
):
    _enable_argocd(monkeypatch)
    for i in range(5):
        make_cache_row(db_session, env="staging", project="normal", application=f"svc-{i}")
    user = make_user(db_session, email="normal-user@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=user)

    patch_calls = []
    _stub_deployment_tree(monkeypatch, patch_calls)

    for i in range(5):
        resp = client.post(
            "/staging/restart",
            data={"project": "normal", "application": f"svc-{i}"},
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True, f"Restart app thu {i + 1} (duoi nguong 10) khong duoc bi chan oan"

    assert len(patch_calls) == 5


# ---------------------------------------------------------------------------
# B5. Khi bi chan boi limiter, request KHONG phai AJAX (khong co header Accept:
# application/json) phai redirect 303 kem flash (KHONG phai 429 tren body redirect,
# _redirect() giu nguyen hanh vi redirect cu, chi JSONResponse moi mang status_code 429).
# ---------------------------------------------------------------------------


def test_rate_limit_blocked_non_ajax_request_redirects_303_with_flash(client_factory, db_session, monkeypatch):
    _enable_argocd(monkeypatch)
    for i in range(11):
        make_cache_row(db_session, env="production", project="nonajax", application=f"app-{i}")
    user = make_user(db_session, email="nonajax-user@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=user)

    patch_calls = []
    _stub_deployment_tree(monkeypatch, patch_calls)

    for i in range(10):
        resp = client.post(
            "/production/restart",
            data={"project": "nonajax", "application": f"app-{i}"},
            follow_redirects=False,
        )
        assert resp.status_code == 303, "Request khong AJAX hop le van redirect 303 nhu cu"

    resp11 = client.post(
        "/production/restart",
        data={"project": "nonajax", "application": "app-10"},
        follow_redirects=False,
    )
    assert resp11.status_code == 303, "Bi chan boi limiter nhung KHONG phai AJAX -> van redirect 303 (khong phai 429 tren response nay)"
    assert resp11.headers["location"] == "/production"


# ---------------------------------------------------------------------------
# B6. [LOW - da va] _APP_NAME_RE dung fullmatch() thay vi match(): 1 gia tri co newline o
# cuoi (vd "app\n") phai bi TU CHOI - truoc ban va, match() van khop "^...$" truoc "\n" cuoi
# chuoi du input KHONG khop toan bo chuoi.
# ---------------------------------------------------------------------------


def test_application_name_with_trailing_newline_is_rejected_by_fullmatch(client_factory, db_session, monkeypatch):
    _enable_argocd(monkeypatch)
    make_cache_row(db_session, env="production", project="core", application="api")
    user = make_user(db_session, email="fullmatch-user@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=user)

    called = {"get": False, "patch": False}
    monkeypatch.setattr(argocd_service.httpx, "get", lambda *a, **k: called.__setitem__("get", True) or FakeResponse({}))
    monkeypatch.setattr(argocd_service.httpx, "post", lambda *a, **k: called.__setitem__("patch", True) or FakeResponse({}))

    resp = client.post(
        "/production/restart",
        data={"project": "core", "application": "api\n"},
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False, "application='api\\n' phai bi tu choi boi regex (fullmatch), khong duoc lot qua whitelist"
    assert body["message"] == "Tên project/application không hợp lệ", body
    assert called["get"] is False and called["patch"] is False


# ---------------------------------------------------------------------------
# B7. [Do tin cay test suite] conftest.py::client_factory phai reset
# restart_service._restart_attempt_state ve rong truoc MOI test - neu khong, 1 user_id da
# dung het 10 luot o 1 test truoc do se lam test SAU (dung lai CUNG user_id, vd cung email
# mac dinh) bi 429 oan tu request DAU TIEN.
# ---------------------------------------------------------------------------


def test_restart_attempt_state_is_reset_between_independent_tests(client_factory, db_session, monkeypatch):
    user = make_user(db_session, email="attempt-state-leak-check@example.com", role=Role.SUPER_ADMIN)
    assert user.id not in restart_service._restart_attempt_state, (
        "conftest.py::client_factory phai reset restart_service._restart_attempt_state ve "
        "rong truoc MOI test - neu user.id nay da co san o day, state dang ro ri giua cac "
        "test doc lap (co the do 1 test khac tinh co tao user cung id truoc do)"
    )

    _enable_argocd(monkeypatch)
    make_cache_row(db_session, env="staging", project="leak2", application="svc")
    client = client_factory(user=user)

    patch_calls = []
    _stub_deployment_tree(monkeypatch, patch_calls)

    resp = client.post(
        "/staging/restart", data={"project": "leak2", "application": "svc"}, headers={"Accept": "application/json"}
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True, (
        "Request Restart dau tien cua 1 user hoan toan moi phai thanh cong - neu bi 429 ngay "
        "lap tuc, nghia la _restart_attempt_state dang ro ri tu test khac va conftest.py "
        "chua reset dung"
    )
