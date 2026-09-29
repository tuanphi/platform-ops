"""[TEST] Việc 3 (phần app/services/argocd_service.py): `get_running_app_status`/
`ArgoAppStatus.is_healthy_and_synced` - dev thêm health/sync để `job_check_running_done`
(app/scheduler.py) KHÔNG còn báo Done chỉ dựa vào version image (trước đây có thể báo Done
SAI khi pod đang ImagePullBackOff/CrashLoopBackOff/Pending).

Dev KHÔNG xác minh được bằng ArgoCD thật (docstring argocd_service.py) - test này tự
dựng response giả theo đúng schema Application resource của ArgoCD (status.summary.images,
status.health.status, status.sync.status) để rà edge case: ArgoCD tắt, lỗi mạng/timeout,
response thiếu field (ArgoCD phiên bản khác/response lạ).

Monkeypatch `argocd_service.httpx.get` (KHÔNG gọi mạng thật) + `argocd_service.settings`
(instance Settings, mutable field assignment) theo đúng tinh thần cô lập DB/network của
tests/conftest.py."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.services import argocd_service


class FakeResponse:
    def __init__(self, json_data=None, raise_exc=None):
        self._json_data = json_data or {}
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc is not None:
            raise self._raise_exc

    def json(self):
        return self._json_data


def _enable_argocd(monkeypatch, url="https://argocd.example.com/api/v1/applications"):
    monkeypatch.setattr(argocd_service.settings, "argocd_url_api", url)
    monkeypatch.setattr(argocd_service.settings, "argocd_token", "fake-token")


def _argo_response(health="Healthy", sync="Synced", version="v1.2.3", images_key_missing=False, health_key_missing=False, sync_key_missing=False):
    status: dict = {}
    if not images_key_missing:
        status["summary"] = {"images": [f"registry.example.com/core/api:{version}"]}
    if not health_key_missing:
        status["health"] = {"status": health}
    if not sync_key_missing:
        status["sync"] = {"status": sync}
    return {"status": status}


# ---------------------------------------------------------------------------
# ArgoCD TẮT (argocd_url_api rỗng) -> None, không crash, không gọi mạng
# ---------------------------------------------------------------------------


def test_returns_none_when_argocd_disabled(db_session, monkeypatch):
    monkeypatch.setattr(argocd_service.settings, "argocd_url_api", "")
    called = []
    monkeypatch.setattr(argocd_service.httpx, "get", lambda *a, **k: called.append(1))

    result = argocd_service.get_running_app_status(db_session, "core", "api")

    assert result is None
    assert called == [], "Khong duoc goi mang khi ArgoCD dang tat"


# ---------------------------------------------------------------------------
# Response đầy đủ, đúng version + Healthy + Synced -> is_healthy_and_synced True
# ---------------------------------------------------------------------------


def test_full_healthy_synced_response_parsed_correctly(db_session, monkeypatch):
    _enable_argocd(monkeypatch)
    monkeypatch.setattr(
        argocd_service.httpx,
        "get",
        lambda *a, **k: FakeResponse(_argo_response(health="Healthy", sync="Synced", version="v1.2.3")),
    )

    result = argocd_service.get_running_app_status(db_session, "core", "api")

    assert result is not None
    assert result.image_version == "v1.2.3"
    assert result.health_status == "Healthy"
    assert result.sync_status == "Synced"
    assert result.is_healthy_and_synced is True


# ---------------------------------------------------------------------------
# Ma trận health/sync x version - is_healthy_and_synced CHỈ True ở đúng 1 tổ hợp
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "health,sync,expected",
    [
        ("Healthy", "Synced", True),
        ("Progressing", "Synced", False),
        ("Degraded", "Synced", False),
        ("Healthy", "OutOfSync", False),
        ("Missing", "Synced", False),
        ("Unknown", "Synced", False),
        ("Healthy", "Unknown", False),
        (None, "Synced", False),
        ("Healthy", None, False),
    ],
)
def test_is_healthy_and_synced_matrix(health, sync, expected):
    status = argocd_service.ArgoAppStatus(image_version="v1", health_status=health, sync_status=sync)
    assert status.is_healthy_and_synced is expected, f"health={health} sync={sync}"


# ---------------------------------------------------------------------------
# Gọi ArgoCD lỗi mạng/timeout -> None, không crash
# ---------------------------------------------------------------------------


def test_network_error_returns_none_and_does_not_raise(db_session, monkeypatch):
    _enable_argocd(monkeypatch)

    def _raise_network_error(*a, **k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(argocd_service.httpx, "get", _raise_network_error)

    result = argocd_service.get_running_app_status(db_session, "core", "api")

    assert result is None


def test_timeout_returns_none_and_does_not_raise(db_session, monkeypatch):
    _enable_argocd(monkeypatch)

    def _raise_timeout(*a, **k):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(argocd_service.httpx, "get", _raise_timeout)

    result = argocd_service.get_running_app_status(db_session, "core", "api")

    assert result is None


def test_http_error_status_code_returns_none_and_does_not_raise(db_session, monkeypatch):
    """vd ArgoCD tra 404 (Application chua ton tai)/401/500 - raise_for_status() raise
    HTTPStatusError, phai duoc bat lai thanh None, khong crash job."""
    _enable_argocd(monkeypatch)
    monkeypatch.setattr(
        argocd_service.httpx,
        "get",
        lambda *a, **k: FakeResponse(
            raise_exc=httpx.HTTPStatusError("404", request=None, response=SimpleNamespace(status_code=404))
        ),
    )

    result = argocd_service.get_running_app_status(db_session, "core", "api")

    assert result is None


# ---------------------------------------------------------------------------
# BUG CANDIDATE: response 200 OK nhưng body KHÔNG phải JSON hợp lệ (vd reverse proxy/LB
# trả trang lỗi HTML dù status 200, hoặc ArgoCD API gateway lỗi) -> resp.json() raise
# json.JSONDecodeError (subclass ValueError, KHÔNG phải httpx.HTTPError) - except
# httpx.HTTPError trong get_running_app_status KHÔNG bắt được ngoại lệ này => hàm CRASH
# thay vì trả None như hợp đồng docstring đã cam kết ("caller PHẢI coi None là chưa biết
# trạng thái thật, KHÔNG được suy diễn"). Vì job_check_running_done/job_check_running_too_long
# KHÔNG có try/except riêng quanh lời gọi get_running_app_status cho từng task, ngoại lệ
# này sẽ làm dừng NGANG vòng lặp của job đó, khiến các task Running khác trong CÙNG lượt
# quét bị bỏ sót luôn (không phải chỉ riêng task lỗi) - báo Leader để chuyển Developer.
# ---------------------------------------------------------------------------


class _InvalidJsonResponse:
    def raise_for_status(self):
        return None

    def json(self):
        import json as _json

        raise _json.JSONDecodeError("Expecting value", "<html>502 Bad Gateway</html>", 0)


def test_non_json_response_body_does_not_crash_get_running_app_status(db_session, monkeypatch):
    _enable_argocd(monkeypatch)
    monkeypatch.setattr(argocd_service.httpx, "get", lambda *a, **k: _InvalidJsonResponse())

    # Ky vong theo docstring: KHONG duoc raise, phai tra ve None (chua biet trang thai
    # that). Neu test nay FAIL (raise JSONDecodeError) tuc la co bug that - bao Developer.
    result = argocd_service.get_running_app_status(db_session, "core", "api")

    assert result is None


# ---------------------------------------------------------------------------
# Response thiếu field (ArgoCD phiên bản khác/response lạ) -> KHÔNG crash, KHÔNG suy diễn
# healthy/synced nhầm
# ---------------------------------------------------------------------------


def test_missing_status_health_field_does_not_crash_and_is_not_healthy(db_session, monkeypatch):
    _enable_argocd(monkeypatch)
    monkeypatch.setattr(
        argocd_service.httpx,
        "get",
        lambda *a, **k: FakeResponse(_argo_response(health_key_missing=True)),
    )

    result = argocd_service.get_running_app_status(db_session, "core", "api")

    assert result is not None, "Van phai tra ve status (co image_version/sync) chu khong None toan bo"
    assert result.health_status is None
    assert result.is_healthy_and_synced is False, "Thieu health status KHONG duoc suy dien la Healthy"


def test_missing_status_sync_field_does_not_crash_and_is_not_healthy(db_session, monkeypatch):
    _enable_argocd(monkeypatch)
    monkeypatch.setattr(
        argocd_service.httpx,
        "get",
        lambda *a, **k: FakeResponse(_argo_response(sync_key_missing=True)),
    )

    result = argocd_service.get_running_app_status(db_session, "core", "api")

    assert result is not None
    assert result.sync_status is None
    assert result.is_healthy_and_synced is False


def test_missing_images_field_returns_none_version_not_crash(db_session, monkeypatch):
    _enable_argocd(monkeypatch)
    monkeypatch.setattr(
        argocd_service.httpx,
        "get",
        lambda *a, **k: FakeResponse(_argo_response(images_key_missing=True)),
    )

    result = argocd_service.get_running_app_status(db_session, "core", "api")

    assert result is not None
    assert result.image_version is None


def test_empty_status_object_entirely_does_not_crash(db_session, monkeypatch):
    """Response la {} rong toan bo (vd ArgoCD tra ve schema hoan toan khac/loi la) -
    khong duoc raise KeyError/AttributeError."""
    _enable_argocd(monkeypatch)
    monkeypatch.setattr(argocd_service.httpx, "get", lambda *a, **k: FakeResponse({}))

    result = argocd_service.get_running_app_status(db_session, "core", "api")

    assert result is not None
    assert result.image_version is None
    assert result.health_status is None
    assert result.sync_status is None
    assert result.is_healthy_and_synced is False


def test_status_key_entirely_missing_from_response_does_not_crash(db_session, monkeypatch):
    _enable_argocd(monkeypatch)
    monkeypatch.setattr(argocd_service.httpx, "get", lambda *a, **k: FakeResponse({"metadata": {}}))

    result = argocd_service.get_running_app_status(db_session, "core", "api")

    assert result is not None
    assert result.image_version is None
    assert result.is_healthy_and_synced is False


def test_status_null_explicitly_in_json_does_not_crash(db_session, monkeypatch):
    """`status` co the la `null` tuong minh trong JSON (khong phai thieu key) - `data.get
    ("status", {}) or {}` trong argocd_service phai xu ly duoc ca truong hop nay."""
    _enable_argocd(monkeypatch)
    monkeypatch.setattr(argocd_service.httpx, "get", lambda *a, **k: FakeResponse({"status": None}))

    result = argocd_service.get_running_app_status(db_session, "core", "api")

    assert result is not None
    assert result.image_version is None
    assert result.is_healthy_and_synced is False


# ---------------------------------------------------------------------------
# get_running_image_version - wrapper tương thích ngược
# ---------------------------------------------------------------------------


def test_get_running_image_version_wrapper_returns_version_only(db_session, monkeypatch):
    _enable_argocd(monkeypatch)
    monkeypatch.setattr(
        argocd_service.httpx,
        "get",
        lambda *a, **k: FakeResponse(_argo_response(version="v9.9.9")),
    )

    version = argocd_service.get_running_image_version(db_session, "core", "api")

    assert version == "v9.9.9"


def test_get_running_image_version_wrapper_returns_none_when_argocd_disabled(db_session, monkeypatch):
    monkeypatch.setattr(argocd_service.settings, "argocd_url_api", "")

    version = argocd_service.get_running_image_version(db_session, "core", "api")

    assert version is None


# ---------------------------------------------------------------------------
# [TEST vòng xác nhận cuối] Nhiệm vụ 1 - các kiểu response bất thường khác dev nói đã
# phòng (top-level không phải dict, status sai kiểu, summary.images sai kiểu/phần tử sai
# kiểu) - PHẢI không crash, không suy diễn Done nhầm.
# ---------------------------------------------------------------------------


def test_top_level_json_is_list_not_object_returns_none_not_crash(db_session, monkeypatch):
    """resp.json() parse thanh cong nhung tra ve list (vd body la "[]" hoac 1 mang) - khong
    phai loi httpx/JSONDecodeError nen phai tu kiem tra rieng."""
    _enable_argocd(monkeypatch)
    monkeypatch.setattr(argocd_service.httpx, "get", lambda *a, **k: FakeResponse([1, 2, 3]))

    result = argocd_service.get_running_app_status(db_session, "core", "api")

    assert result is None


def test_top_level_json_is_string_not_object_returns_none_not_crash(db_session, monkeypatch):
    _enable_argocd(monkeypatch)
    monkeypatch.setattr(argocd_service.httpx, "get", lambda *a, **k: FakeResponse("just a string"))

    result = argocd_service.get_running_app_status(db_session, "core", "api")

    assert result is None


def test_status_field_is_list_not_object_returns_none_not_crash(db_session, monkeypatch):
    """data la dict hop le nhung data["status"] lai la list (schema la, vd ArgoCD phien
    ban khac) - phai bi bat boi nhanh isinstance(status, dict), khong duoc goi .get() truc
    tiep tren list (se AttributeError khong duoc bat neu thieu check)."""
    _enable_argocd(monkeypatch)
    monkeypatch.setattr(argocd_service.httpx, "get", lambda *a, **k: FakeResponse({"status": [1, 2, 3]}))

    result = argocd_service.get_running_app_status(db_session, "core", "api")

    assert result is None


def test_summary_field_is_string_not_object_returns_none_not_crash(db_session, monkeypatch):
    """status["summary"] la string thay vi dict - status.get("summary", {}).get("images")
    se AttributeError ('str' object has no attribute 'get') - phai duoc bat, tra None."""
    _enable_argocd(monkeypatch)
    body = {"status": {"summary": "oops-not-a-dict", "health": {"status": "Healthy"}, "sync": {"status": "Synced"}}}
    monkeypatch.setattr(argocd_service.httpx, "get", lambda *a, **k: FakeResponse(body))

    result = argocd_service.get_running_app_status(db_session, "core", "api")

    assert result is None


def test_summary_images_is_dict_not_list_returns_none_not_crash(db_session, monkeypatch):
    """summary.images la dict thay vi list - images[0] se KeyError (dict lookup key 0) -
    phai duoc bat, tra None, KHONG duoc suy dien Done."""
    _enable_argocd(monkeypatch)
    body = {
        "status": {
            "summary": {"images": {"unexpected": "shape"}},
            "health": {"status": "Healthy"},
            "sync": {"status": "Synced"},
        }
    }
    monkeypatch.setattr(argocd_service.httpx, "get", lambda *a, **k: FakeResponse(body))

    result = argocd_service.get_running_app_status(db_session, "core", "api")

    assert result is None


def test_summary_images_element_not_string_returns_none_not_crash(db_session, monkeypatch):
    """summary.images la list nhung phan tu KHONG phai string (vd so nguyen, dict long) -
    images[0].rsplit() se AttributeError - phai duoc bat, tra None."""
    _enable_argocd(monkeypatch)
    body = {
        "status": {
            "summary": {"images": [123456, {"nested": "object"}]},
            "health": {"status": "Healthy"},
            "sync": {"status": "Synced"},
        }
    }
    monkeypatch.setattr(argocd_service.httpx, "get", lambda *a, **k: FakeResponse(body))

    result = argocd_service.get_running_app_status(db_session, "core", "api")

    assert result is None


def test_summary_images_is_string_not_list_does_not_crash_but_yields_garbage_version(db_session, monkeypatch):
    """[GHI NHAN - KHONG phai crash, chi la ket qua sai lech con sot lai] Neu
    summary.images la 1 STRING (thay vi list) thay vi raise, code hien tai VAN chay duoc vi
    string cung ho tro indexing/slicing trong Python: images[0] tra ve 1 KY TU dau tien cua
    chuoi thay vi phan tu dau list, ".rsplit(':', 1)[-1]" tren 1 ky tu don khong loi -> ham
    KHONG crash (dung yeu cau chinh: khong lam dung vong lap) nhung image_version tra ve la
    RAC (vd chi 1 ky tu), khong phai version that.

    Muc do anh huong: job_check_running_done chi Done khi
    `argo_status.image_version == task.image_version` (khop CHINH XAC ca chuoi) nen 1 ky tu
    rac gan nhu khong bao gio trung khop version that -> KHONG gay Done sai (huong an toan,
    chi khien task bi coi la "chua khop version" du thuc te co the da dung). Van la 1 khe ho
    con sot (2 lop isinstance() cua dev CHUA bao phu "summary"/"images" sai kieu, chi bao
    phu "data"/"status") - bao Leader/Dev de tuy quyet dinh co can vá them hay khong, KHONG
    tu ket luan muc do nghiem trong o day."""
    _enable_argocd(monkeypatch)
    body = {
        "status": {
            "summary": {"images": "registry.example.com/core/api:v9.9.9"},
            "health": {"status": "Healthy"},
            "sync": {"status": "Synced"},
        }
    }
    monkeypatch.setattr(argocd_service.httpx, "get", lambda *a, **k: FakeResponse(body))

    result = argocd_service.get_running_app_status(db_session, "core", "api")

    assert result is not None, "Khong duoc crash"
    assert result.image_version != "v9.9.9", (
        "Ghi nhan hanh vi hien tai: image_version bi RAC ('r', ky tu dau chuoi) khi "
        "summary.images la string thay vi list - KHONG phai version that"
    )
    assert result.health_status == "Healthy"
    assert result.sync_status == "Synced"
