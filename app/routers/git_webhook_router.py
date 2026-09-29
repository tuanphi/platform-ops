"""Webhook nhận GitLab push event - khi có push lên đúng nhánh staging/master, tự động
đồng bộ lại catalog Group/Project + cache EnvAppCache (Staging/Production) NGAY thay vì
đợi tới job định kỳ (job vẫn giữ nguyên, làm phương án dự phòng nếu webhook bị mất/GitLab
gọi lỗi). Push từ chính app (Run, Rollback, Bật/Tắt Staging/Production) cũng sẽ trigger
webhook này như push thường - vô hại vì đây là 1 chiều CHỈ ĐỌC git để refresh cache, không
push lại gì cả nên không tạo vòng lặp.

Mỗi sync thành công còn publish qua sync_events (SSE) để trang /catalog, /staging,
/production đang mở tự quay nút Sync/Làm mới + reload, xem app/services/sync_events.py.

Việc sync (git fetch/clone + quét thư mục + ghi DB) chạy trong BackgroundTask, KHÔNG chặn
response trả về GitLab - lần đầu app start (chưa có checkout local, ensure_*_repo() phải
`git clone` toàn bộ thay vì fetch nhanh) có thể mất hàng chục giây với repo lớn, vượt quá
timeout mặc định của GitLab (~10s) và bị báo lỗi "Net::ReadTimeout" nếu chạy đồng bộ trong
request. GitLab chỉ cần nhận 2xx NGAY để coi là "đã nhận", không quan tâm sync xong lúc nào."""

import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.services import catalog_service, production_service, staging_service, sync_events
from app.services.app_setting_service import get_app_setting
from app.services.git_service import GitError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["git"])
settings = get_settings()


def _run_sync(db: Session, label: str, sync_fn) -> bool:
    """Chay 1 sync doc lap - 1 nguon loi (vd repo chua cau hinh, mang loi) khong duoc lam
    fail ca cac sync con lai - giong cach job_sync_env_apps trong scheduler.py bat GitError
    rieng cho tung nhanh."""
    try:
        sync_fn(db)
        sync_events.publish(label)
        return True
    except (GitError, FileNotFoundError) as exc:
        logger.error("Webhook sync %s thất bại: %s", label, exc)
        return False


def _sync_in_background(is_staging: bool, is_master: bool) -> None:
    """Chay SAU khi response da tra ve GitLab (xem BackgroundTasks trong git_push_webhook) -
    tu mo/dong session rieng vi khong con nam trong vong doi cua request goc."""
    db = SessionLocal()
    try:
        if is_staging:
            _run_sync(db, "catalog", catalog_service.sync_catalog_from_chart_repo)
            _run_sync(db, "staging", staging_service.sync_apps)
        if is_master:
            _run_sync(db, "production", production_service.sync_apps)
    finally:
        db.close()


@router.post("/git")
async def git_push_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_gitlab_token: str | None = Header(default=None),
):
    # Fail-closed (giong telegram_webhook): chua cau hinh GIT_WEBHOOK_SECRET thi tu choi
    # luon thay vi bo qua kiem tra - thieu buoc nay, ai cung POST payload gia de trigger
    # sync tuy y (khong nguy hiem bang webhook Telegram vi chi doc git, nhung van la 1
    # endpoint khong xac thuc goi duoc tu ben ngoai).
    if not settings.git_webhook_secret or not hmac.compare_digest(x_gitlab_token or "", settings.git_webhook_secret):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    payload = await request.json()
    ref = payload.get("ref") or ""
    branch = ref.removeprefix("refs/heads/")
    if not branch or branch == ref:
        return {"ok": True, "message": "Không có ref dạng nhánh (refs/heads/...), bỏ qua"}

    # Doc AppSetting la 1 SELECT cuc bo, khong dung git - lam ngay trong request, khong
    # can day vao background (nhanh, khong co rui ro timeout).
    db = SessionLocal()
    try:
        app_setting = get_app_setting(db)
        is_staging = branch == app_setting.git_branch_staging
        is_master = branch == app_setting.git_branch_master
    finally:
        db.close()

    if not is_staging and not is_master:
        return {"ok": True, "message": f"Nhánh '{branch}' không khớp staging/master đang cấu hình, bỏ qua"}

    targets = (["catalog", "staging"] if is_staging else []) + (["production"] if is_master else [])
    background_tasks.add_task(_sync_in_background, is_staging, is_master)
    return {"ok": True, "message": f"Đã nhận, đang đồng bộ nền: {', '.join(targets)}"}
