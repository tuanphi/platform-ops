"""Gửi thông báo Telegram + email. Được gọi qua BackgroundTasks nên không
block response HTTP (FastAPI chạy background task trong threadpool)."""

import html
import logging
import smtplib
from email.mime.text import MIMEText

import httpx
import tabulate as tabulate_lib
from tabulate import tabulate

from app.config import get_settings
from app.models import DeployTask
from app.services.app_setting_service import get_app_setting

# Tat MIN_PADDING (mac dinh 2 - "minimum extra space in headers" cua tabulate) de cac
# cot ngan (vd Project/CommitID) khong bi thua 2 ky tu so voi header - padding deu 1 ben
# trai/phai cho tat ca bang gui qua build_ascii_table (Task done, Need Deploy Now, 30-min
# reminder, bot reply...).
tabulate_lib.MIN_PADDING = 0

logger = logging.getLogger(__name__)
settings = get_settings()


def send_telegram(message: str, chat_id: str | None = None, bot_token: str | None = None) -> None:
    """`bot_token` mac dinh None -> dung settings.telegram_bot_token (bot dung CHUNG cho
    moi luong: deploy, Bat/Tat staging, Production - chi khac chat_id/channel)."""
    token = bot_token if bot_token is not None else settings.telegram_bot_token
    if not token:
        logger.info("[telegram-disabled] %s", message)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id or get_app_setting().telegram_chat_id_system,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = httpx.post(url, json=payload, timeout=10)
        resp.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Gửi Telegram thất bại")


def send_infra_toggle_telegram(message: str, env: str) -> None:
    """Gui thong bao ve hoat dong Bat/Tat staging/Production replicas qua bot CHUNG voi
    luong deploy (settings.telegram_bot_token) nhung MOI kenh (staging/production) co chat
    id + cong tac bat/tat rieng, sua duoc qua UI Settings > General
    (AppSetting.telegram_chat_id_staging/telegram_chat_id_production,
    enable_staging_notify/enable_production_notify). `env` phai la "staging" hoac
    "production" (da duoc chuan hoa boi caller - xem env_toggle_service.notify)."""
    app_setting = get_app_setting()
    if env == "production":
        if not app_setting.enable_production_notify:
            logger.info("[infra-notify-disabled] production %s", message)
            return
        chat_id = app_setting.telegram_chat_id_production
    else:
        if not app_setting.enable_staging_notify:
            logger.info("[infra-notify-disabled] staging %s", message)
            return
        chat_id = app_setting.telegram_chat_id_staging

    send_telegram(message, chat_id=chat_id)


def send_mail(body: str, subject: str | None = None) -> None:
    app_setting = get_app_setting()
    if not app_setting.enable_mail:
        logger.info("[mail-disabled-by-config] subject=%s body=%s", subject, body)
        return
    if not settings.smtp_host or not app_setting.mail_to:
        logger.info("[mail-not-configured] subject=%s body=%s", subject, body)
        return
    recipients = [addr.strip() for addr in app_setting.mail_to.split(",") if addr.strip()]
    msg = MIMEText(body, "html", "utf-8")
    msg["Subject"] = subject or app_setting.mail_subject
    msg["From"] = app_setting.mail_from
    msg["To"] = ", ".join(recipients)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
            server.starttls()
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(app_setting.mail_from, recipients, msg.as_string())
    except (smtplib.SMTPException, OSError):
        logger.exception("Gửi email thất bại")


def task_summary_lines(task: DeployTask, actor_label: str | None = None, actor_email: str | None = None) -> str:
    """Cac field free-text/nguoi dung nhap (email, commitid) PHAI html.escape truoc khi
    noi suy vao message parse_mode=HTML - task.project/application chi den tu catalog
    (Group/Project do Super Admin sync tu git repo), khong phai input tu form nguoi dung
    thuong nen khong can escape."""
    link = f"{settings.app_url}/tasks/{task.id}"
    actor_line = f"<b>- {actor_label}:</b> {html.escape(actor_email)}\n" if actor_label else ""
    return (
        f"<b>- User:</b> {html.escape(task.email)}\n"
        f"<b>- Project:</b> {task.project}\n"
        f"<b>- Application:</b> {task.application}\n"
        f"<b>- CommitID:</b> {html.escape(task.commitid)}\n"
        f"{actor_line}"
        f"<a href='{link}'>Get more info</a>"
    )


def build_ascii_table(rows: list[dict]) -> str:
    """html.escape TUNG GIA TRI truoc khi dua vao tabulate - ket qua duoc nhung trong the
    <pre> roi gui voi parse_mode=HTML (Telegram) hoac nhung nguyen vao email HTML, nen cac
    field co nguon goc tu du lieu nguoi dung (vd task.email) van co the chua ky tu HTML dac
    biet neu khong escape (escape tai 1 diem duy nhat de moi caller hien tai/tuong lai deu
    an toan, khong phai nho tung noi goi tu escape rieng le)."""
    if not rows:
        return "Không có gì"
    headers = list(rows[0].keys())
    escaped_rows = [[html.escape(str(v)) for v in r.values()] for r in rows]
    table = tabulate(escaped_rows, headers=headers, tablefmt="simple_grid")
    return f"<pre>{table}</pre>"
