"""Webhook nhận lệnh 2 chiều từ Telegram bot (không cần mở web để xem list task)."""

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models import DeployTask, TaskStatus
from app.services.app_setting_service import get_app_setting
from app.services.notify_service import build_ascii_table, send_telegram

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["telegram"])
settings = get_settings()


def _allowed_chat_ids(db: Session) -> set[str]:
    """Allowlist chat_id duoc phep nhan du lieu noi bo (danh sach task/commit/status) qua
    webhook - AppSetting.telegram_chat_id_system sua duoc tu UI /settings/general, phan
    cach boi dau phay de ho tro nhieu group chat he thong. Webhook secret token chi xac
    thuc request THAT SU tu Telegram, khong xac thuc AI la nguoi gui lenh trong chat -
    thieu allowlist nay thi bat ky chat_id nao cung xin duoc du lieu noi bo qua /list*."""
    raw = get_app_setting(db).telegram_chat_id_system or ""
    return {chat_id.strip() for chat_id in raw.split(",") if chat_id.strip()}

STATUS_COMMANDS = {
    "/listdone": TaskStatus.DONE,
    "/listapprove": TaskStatus.APPROVED,
    "/listreject": TaskStatus.REJECTED,
    "/listcancelled": TaskStatus.CANCELLED,
    "/listwaiting": TaskStatus.TASK,
    "/listrunning": TaskStatus.RUNNING,
}


def _row(task: DeployTask, time_field: str) -> dict:
    return {
        "Project": task.project,
        "Application": task.application,
        "CommitID": task.commitid,
        time_field: getattr(task, "confirmed_run_at" if time_field == "Run at" else "created_at"),
        "Secret": "yes" if task.config_env else "no",
        "Status": task.status.value,
    }


def _handle_command(db: Session, text: str) -> str:
    text = text.strip().lower()

    if text == "/list":
        tasks = (
            db.query(DeployTask)
            .filter(DeployTask.created_at.between(datetime.utcnow() - timedelta(hours=168), datetime.utcnow() + timedelta(hours=168)))
            .order_by(DeployTask.created_at.desc())
            .limit(35)
            .all()
        )
        title = "🔊 List project deploy 🔊"
        rows = [_row(t, "Create at") for t in tasks]
    elif text in STATUS_COMMANDS:
        status = STATUS_COMMANDS[text]
        tasks = db.query(DeployTask).filter(DeployTask.status == status).order_by(DeployTask.id.desc()).limit(35).all()
        title = f"📋 List task status {status.value} 📋"
        rows = [_row(t, "Run at" if status == TaskStatus.RUNNING else "Create at") for t in tasks]
    else:
        return "Lệnh không hợp lệ. Dùng /list, /listdone, /listapprove, /listreject, /listcancelled, /listwaiting, /listrunning"

    table = build_ascii_table(rows)
    link = f"<a href='{settings.app_url}/tasks'>Get more info</a>"
    return f"<b>{title}</b>\n{table}\n{link}"


@router.post("/sendtelegram")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    # Fail-closed: neu chua cau hinh TELEGRAM_WEBHOOK_SECRET thi tu choi luon thay vi bo
    # qua kiem tra - truoc day thieu secret = mo webhook khong xac thuc, ai cung POST duoc
    # payload gia de bot tra ve danh sach task noi bo cho bat ky chat_id nao ho chi dinh.
    if not settings.telegram_webhook_secret or x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    payload = await request.json()
    message = payload.get("message") or payload.get("edited_message") or {}
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "")

    if not chat_id or not text.startswith("/"):
        return {"ok": True}

    db = SessionLocal()
    try:
        # Allowlist chat_id: secret token o tren chi xac thuc request DEN TU Telegram that,
        # khong xac thuc AI trong chat do dang go lenh - bo qua im lang (khong tra loi gi)
        # thay vi tra du lieu noi bo cho chat_id la, tranh lo danh sach task/commit/status.
        # Fail-closed: allowlist rong (Setting > General chua cau hinh Telegram chat id he
        # thong) nghia la CHUA co chat_id nao duoc phep, khong phai "cho phep tat ca".
        allowed = _allowed_chat_ids(db)
        if str(chat_id) not in allowed:
            logger.warning("Telegram webhook: chat_id %s không thuộc allowlist, bỏ qua lệnh %r", chat_id, text)
            return {"ok": True}
        reply = _handle_command(db, text)
    finally:
        db.close()

    send_telegram(reply, chat_id=str(chat_id))
    return {"ok": True}
