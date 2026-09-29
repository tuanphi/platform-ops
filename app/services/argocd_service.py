"""Gọi ArgoCD API (chỉ đọc) để lấy tình trạng thực tế của 1 Application (version image đang
chạy + health/sync) - dùng response Application resource của ArgoCD (GET
/api/v1/applications/{name}), cấu trúc dựa trên CRD argoproj.io/v1alpha1.Application:
  - status.summary.images[]: danh sách image (dạng "registry/.../app:tag") có trong
    manifest ĐÃ ÁP DỤNG - KHÔNG đảm bảo pod đã chạy khoẻ (có thể đang ImagePullBackOff/
    CrashLoopBackOff/Pending/chưa qua readiness probe dù version đã khớp).
  - status.health.status: "Healthy" | "Progressing" | "Degraded" | "Suspended" | "Missing" |
    "Unknown" - tình trạng SỨC KHOẺ thật của resource (pod/deployment...) do ArgoCD tự đánh
    giá qua health check tiêu chuẩn của từng loại resource.
  - status.sync.status: "Synced" | "OutOfSync" | "Unknown" - manifest trên git đã khớp với
    cluster hay chưa.
GIẢ ĐỊNH (không gọi được ArgoCD thật trong môi trường phát triển repo này để xác minh trực
tiếp - dựa theo tài liệu/schema chính thức của ArgoCD, cần DevOps xác nhận lại với 1 lần gọi
API thật trước khi tin tưởng hoàn toàn nếu version ArgoCD đang dùng khác biệt đáng kể)."""

import json
import logging
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.services.app_setting_service import get_app_setting

logger = logging.getLogger(__name__)
settings = get_settings()


@dataclass
class ArgoAppStatus:
    image_version: str | None
    health_status: str | None
    sync_status: str | None

    @property
    def is_healthy_and_synced(self) -> bool:
        """True CHI KHI ca health VA sync deu da dat trang thai tot - dung de quyet dinh
        task co thuc su Done hay chua (xem scheduler.py::job_check_running_done), KHONG chi
        dua vao version image da khop nhu truoc day."""
        return self.health_status == "Healthy" and self.sync_status == "Synced"


def get_running_app_status(db: Session, project: str, application: str) -> ArgoAppStatus | None:
    """Tra ve None khi ArgoCD dang TAT (settings.argocd_url_api rong) hoac goi API that
    bai/loi mang - caller PHAI coi None la "chua biet duoc trang thai that", KHONG duoc suy
    dien la Done/Healthy (giu nguyen hanh vi tuong thich nguoc cua ham get_running_image_version
    cu: moi truong khong dung ArgoCD se KHONG bao gio tu dong chuyen task sang Done qua
    job_check_running_done, giong het truoc day)."""
    if not settings.argocd_url_api:
        logger.info("[argocd-disabled] bỏ qua kiểm tra trạng thái cho %s/%s", project, application)
        return None

    app_setting = get_app_setting(db)
    app_name = f"{project}-{application}{app_setting.argocd_application_postfix}"
    # quote(safe="") ma hoa CA "/" - app_name duoc ghep truc tiep tu project/application do
    # nguoi dung nhap (form Restart/trang deploy), neu khong ma hoa 1 gia tri chua "/" hoac
    # ".." co the lam lech URL PATH thuc te, cham 1 endpoint ArgoCD khac duoi cung service
    # token quyen cao (xem them regex validate o restart_service.restart_app - defense in
    # depth, ca hai lop deu phai co).
    url = f"{settings.argocd_url_api.rstrip('/')}/{quote(app_name, safe='')}"
    headers = {"Authorization": f"Bearer {settings.argocd_token}"}

    try:
        resp = httpx.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            # resp.json() co the parse thanh cong nhung tra ve list/str/number... (vd
            # response 200 nhung body la mang rong "[]" hoac 1 chuoi don gian) - khong
            # phai loi httpx/JSONDecodeError nen phai tu kiem tra rieng, KHONG duoc goi
            # .get() truc tiep tren gia tri khong phai dict (se AttributeError).
            raise ValueError(f"ArgoCD tra ve JSON khong phai object (nhan duoc {type(data).__name__})")
        status = data.get("status", {}) or {}
        if not isinstance(status, dict):
            raise ValueError(f"ArgoCD tra ve status khong phai object (nhan duoc {type(status).__name__})")
        # images co the co nhieu phan tu neu Application co nhieu container/image (vd
        # sidecar) - CHI lay images[0] (gioi han da co tu truoc, GIU NGUYEN o day: sap xep
        # lai theo dung ten container de so sanh chinh xac tung image doi hoi biet truoc
        # thu tu/ten container chuan cua chart, chua co quy uoc chung trong repo nay -
        # xem bao cao [dev] de Leader/DevOps quyet dinh co can chuan hoa hay khong).
        images = status.get("summary", {}).get("images", []) or []
        image_version = images[0].rsplit(":", 1)[-1] if images else None
        health_status = (status.get("health", {}) or {}).get("status")
        sync_status = (status.get("sync", {}) or {}).get("status")
        return ArgoAppStatus(image_version=image_version, health_status=health_status, sync_status=sync_status)
    except (httpx.HTTPError, ValueError, TypeError, AttributeError, KeyError):
        # httpx.HTTPError: loi mang/timeout/status code loi (vd 404/401/500).
        # ValueError: bao gom json.JSONDecodeError - response 200 OK nhung body KHONG
        # phai JSON hop le (vd reverse proxy/API gateway/load balancer tra trang loi HTML
        # kem status 200 do cau hinh ingress sai/timeout tang proxy) - da tung crash ca
        # vong lap job_check_running_done/job_check_running_too_long, xem scheduler.py.
        # TypeError/AttributeError/KeyError: phong ngua schema response bat thuong khac
        # (vd field co gia tri sai kieu du liệu du data la dict, response tu ArgoCD phien
        # ban khac) ma isinstance() o tren khong bat het duoc - khong de bat ky loi parse
        # nao lam crash ca job, dung theo dung hop dong docstring: caller PHAI coi None la
        # chua biet trang thai that, khong duoc suy dien.
        logger.exception("Gọi ArgoCD API thất bại hoặc response không hợp lệ cho %s", app_name)
        return None


def get_running_image_version(db: Session, project: str, application: str) -> str | None:
    """Giu lai cho tuong thich nguoc voi cac noi CHI can version (khong can health/sync) -
    wrapper mong quanh get_running_app_status."""
    status = get_running_app_status(db, project, application)
    return status.image_version if status is not None else None


@dataclass
class WorkloadRestartResult:
    ok: bool
    message: str
    workload_kind: str | None = None
    workload_name: str | None = None


_RESTARTABLE_KINDS = ("Deployment", "StatefulSet")
_RESTART_ACTION_NAME = "restart"


def restart_workload(db: Session, env_label: str, project: str, application: str) -> WorkloadRestartResult:
    """Khoi dong lai (rolling restart) workload cua 1 Application ArgoCD - tuong duong
    `kubectl rollout restart deploy/sts <name>` nhung THUC HIEN HOAN TOAN QUA ArgoCD HTTP
    API (KHONG dung kubeconfig/kubectl truc tiep, dung quyet dinh kien truc da chot).

    CO CHE: goi API "Resource Actions" cua ArgoCD server voi action="restart" - ArgoCD
    da co SAN Lua resource customization cho action nay voi CA 2 kind Deployment va
    StatefulSet (xem resource_customizations/apps/Deployment|StatefulSet/actions/restart/
    action.lua trong source argoproj/argo-cd - script patch annotation
    kubectl.kubernetes.io/restartedAt vao spec.template.metadata.annotations, CHINH LA co
    che dung sau `kubectl rollout restart`), KHONG can cluster tu cau hinh them gi trong
    ConfigMap argocd-cm. Day CUNG LA nut "Restart" co san tren giao dien web ArgoCD khi
    xem 1 Deployment/StatefulSet.

    SUA doi 2026-08-18 (incident that tren production): ban dau dung API "PatchResource"
    (tu ghep annotation patch, POST .../resource?...) - bi ArgoCD tu choi 403 Forbidden vi
    RBAC policy thuc te cua to chuc (vd `p, role:developer, applications,
    action/apps/Deployment/restart, *-dev/*, allow`) CHI cap quyen cho dung ACTION
    "restart" (RBAC resource "applications", action "action/<group>/<kind>/<action>"),
    KHONG cap quyen "update"/"patch" chung ma PatchResource can - vi vay PHAI dung dung API
    Resource Actions nay, khop chinh xac RBAC action string ma admin ArgoCD da cau hinh.

    3 buoc goi ArgoCD API (dung 1 shared service token settings.argocd_token, KHONG xay
    per-user auth):
      1) GET /api/v1/applications/{appName}/resource-tree - liet ke moi resource con cua
         Application, tim node co kind Deployment/StatefulSet DAU TIEN (gia dinh moi app
         CHI CO 1 workload chinh - neu co nhieu, se restart node dau tien quet duoc, CHUA
         co co che chon giua nhieu workload).
      2) app_name duoc ghep VOI HAU TO RIENG theo env_label ("staging" ->
         app_setting.argocd_application_postfix_staging, "production" ->
         ..._production) - KHAC voi app_setting.argocd_application_postfix (postfix CHUNG,
         chi dung cho tinh nang Running/DeployTask trong scheduler.py). Dung 1 postfix
         chung cho ca Staging lan Production se lam ca 2 nut Restart cung tro vao 1 Application
         ArgoCD (bug thuc te gap phai, incident 2026-08-18) - vi ArgoCD non-prod co the anh
         xa Staging/Production trong form-deploy toi 2 moi truong ArgoCD khac nhau (vd
         "-dev" va "-sandbox").
      3) POST /api/v1/applications/{appName}/resource/actions?namespace=...&resourceName=...
         &version=...&group=...&kind=... voi body la RunResourceAction RPC (API v1,
         KHONG dung v2 /resource/actions/v2 vi can it tham so hon - restart khong can
         resourceActionParameters - v1 tuong thich nguoc voi ArgoCD cu hon).

         XAC NHAN qua server/application/application.proto + assets/swagger.json chinh
         thuc argoproj/argo-cd: RunResourceAction anh xa HTTP POST, body directive
         `body: "action"` voi schema "type": "string" - CUNG hop dong nhu PatchResource
         (body phai la 1 JSON STRING LITERAL boc quanh ten action, vd '"restart"', KHONG
         phai JSON object).

    GIA DINH CHUA XAC MINH VOI ArgoCD THAT: cau truc JSON tra ve tu /resource-tree co
    field "nodes" la mang object co cac khoa "kind"/"name"/"namespace"/"group"/"version" o
    CAP CAO NHAT cua tung phan tu, dung theo proto ResourceNode cua ArgoCD, KHONG long
    trong object con."""
    if not settings.argocd_url_api:
        logger.info("[argocd-disabled] bỏ qua restart cho %s/%s", project, application)
        return WorkloadRestartResult(ok=False, message="ArgoCD chưa được cấu hình (ARGOCD_URL_API rỗng), không thể restart")

    app_setting = get_app_setting(db)
    postfix = (
        app_setting.argocd_application_postfix_staging
        if env_label == "staging"
        else app_setting.argocd_application_postfix_production
    )
    app_name = f"{project}-{application}{postfix}"
    # Ma hoa app_name truoc khi ghep vao URL PATH - cung ly do nhu get_running_app_status o
    # tren (khong duoc bo sot o day, day la ham thuc su MUTATE ArgoCD).
    encoded_app_name = quote(app_name, safe="")
    base_url = settings.argocd_url_api.rstrip("/")
    headers = {"Authorization": f"Bearer {settings.argocd_token}"}

    try:
        tree_resp = httpx.get(f"{base_url}/{encoded_app_name}/resource-tree", headers=headers, timeout=10)
        tree_resp.raise_for_status()
        tree = tree_resp.json()
        if not isinstance(tree, dict):
            raise ValueError(f"ArgoCD tra ve resource-tree khong phai object (nhan duoc {type(tree).__name__})")
        nodes = tree.get("nodes", []) or []
        if not isinstance(nodes, list):
            raise ValueError(f"ArgoCD tra ve resource-tree.nodes khong phai list (nhan duoc {type(nodes).__name__})")

        target = None
        for node in nodes:
            if isinstance(node, dict) and node.get("kind") in _RESTARTABLE_KINDS:
                target = node
                break
        if target is None:
            return WorkloadRestartResult(
                ok=False,
                message=f"Không tìm thấy Deployment/StatefulSet nào cho ứng dụng {app_name} trên ArgoCD",
            )

        kind = target.get("kind")
        name = target.get("name")
        namespace = target.get("namespace")
        group = target.get("group") or "apps"
        version = target.get("version") or "v1"
        if not name or not namespace:
            return WorkloadRestartResult(
                ok=False,
                message=f"Dữ liệu resource-tree của {app_name} thiếu name/namespace, không thể restart",
                workload_kind=kind,
            )

        params = {
            "namespace": namespace,
            "resourceName": name,
            "version": version,
            "group": group,
            "kind": kind,
        }
        action_resp = httpx.post(
            f"{base_url}/{encoded_app_name}/resource/actions",
            params=params,
            headers={**headers, "Content-Type": "application/json"},
            content=json.dumps(_RESTART_ACTION_NAME),
            timeout=10,
        )
        action_resp.raise_for_status()
        return WorkloadRestartResult(ok=True, message=f"Đã khởi động lại {kind}/{name}", workload_kind=kind, workload_name=name)
    except (httpx.HTTPError, ValueError, TypeError, AttributeError, KeyError):
        # Cung dieu kien fail-safe nhu get_running_app_status: KHONG de bat ky loi
        # mang/parse/schema bat thuong nao lam crash request cua nguoi dung (AC8) - log
        # day du (KHONG log headers/token) de dieu tra, tra ve message than thien.
        logger.exception("Restart workload qua ArgoCD thất bại cho %s", app_name)
        return WorkloadRestartResult(ok=False, message=f"Gọi ArgoCD API để restart thất bại cho {app_name}")
