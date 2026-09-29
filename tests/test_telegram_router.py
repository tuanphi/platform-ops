"""Kiểm thử allowlist chat_id trong app/routers/telegram_router.py (fix B4).

Bug gốc: webhook secret token (x-telegram-bot-api-secret-token) chỉ xác thực
request THẬT SỰ đến từ Telegram, không xác thực AI đang gõ lệnh trong chat đó -
thiếu allowlist AppSetting.telegram_chat_id_system thì bất kỳ chat_id nào cũng
lấy được danh sách task/commit/status nội bộ qua /list*. Fix thêm allowlist
fail-closed (rỗng = chưa cho phép ai, không phải cho phép tất cả).

Router tự mở/đóng session bằng `SessionLocal()` import trực tiếp (không qua
Depends(get_db)) nên không dùng lại được conftest.client_factory - test ở đây tự
monkeypatch telegram_router.SessionLocal sang db_session của test."""

import pytest
from fastapi.testclient import TestClient

from app.services.app_setting_service import update_app_setting
from tests.conftest import make_user  # noqa: F401  (đảm bảo conftest đã import xong models)


WEBHOOK_SECRET = "test-webhook-secret-value"


@pytest.fixture()
def telegram_client(db_session, monkeypatch):
    import app.main as main_module
    from app.routers import telegram_router
    from app.services import notify_service

    monkeypatch.setattr(main_module, "start_scheduler", lambda: None)
    monkeypatch.setattr(main_module, "shutdown_scheduler", lambda: None)

    monkeypatch.setattr(telegram_router, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(telegram_router.settings, "telegram_webhook_secret", WEBHOOK_SECRET)

    sent = []
    monkeypatch.setattr(
        notify_service,
        "send_telegram",
        lambda message, chat_id=None: sent.append({"message": message, "chat_id": chat_id}),
    )
    # telegram_router import send_telegram trực tiếp vào namespace của nó
    monkeypatch.setattr(telegram_router, "send_telegram", notify_service.send_telegram)

    client = TestClient(main_module.app)
    client.sent = sent  # type: ignore[attr-defined]
    return client


def _payload(chat_id, text="/list"):
    return {"message": {"chat": {"id": chat_id}, "text": text}}


def _post(client, payload, secret=WEBHOOK_SECRET):
    headers = {}
    if secret is not None:
        headers["x-telegram-bot-api-secret-token"] = secret
    return client.post("/webhook/sendtelegram", json=payload, headers=headers)


# ---------------------------------------------------------------------------
# Webhook secret token vẫn phải được kiểm tra (lớp bảo vệ trước allowlist)
# ---------------------------------------------------------------------------


def test_webhook_rejects_request_without_secret_header(telegram_client):
    resp = _post(telegram_client, _payload(123), secret=None)
    assert resp.status_code == 403
    assert telegram_client.sent == []


def test_webhook_rejects_request_with_wrong_secret(telegram_client):
    resp = _post(telegram_client, _payload(123), secret="wrong-secret")
    assert resp.status_code == 403
    assert telegram_client.sent == []


# ---------------------------------------------------------------------------
# Allowlist chat_id
# ---------------------------------------------------------------------------


def test_unlisted_chat_id_gets_no_internal_data(telegram_client, db_session):
    update_app_setting(db_session, telegram_chat_id_system="111111111")

    resp = _post(telegram_client, _payload(999999999, "/list"))

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert telegram_client.sent == []  # không có reply nào được gửi cho chat lạ


def test_allowlisted_chat_id_receives_reply(telegram_client, db_session):
    update_app_setting(db_session, telegram_chat_id_system="123456789")

    resp = _post(telegram_client, _payload(123456789, "/list"))

    assert resp.status_code == 200
    assert len(telegram_client.sent) == 1
    assert telegram_client.sent[0]["chat_id"] == "123456789"
    assert "List project deploy" in telegram_client.sent[0]["message"]


def test_empty_allowlist_fails_closed_for_everyone(telegram_client, db_session):
    """AppSetting.telegram_chat_id_system chưa được cấu hình (rỗng) - fail-closed:
    KHÔNG chat_id nào được xem là hợp lệ, kể cả khi webhook secret đúng."""
    update_app_setting(db_session, telegram_chat_id_system="")

    resp = _post(telegram_client, _payload(123456789, "/list"))

    assert resp.status_code == 200
    assert telegram_client.sent == []


def test_allowlist_parses_comma_separated_ids_with_whitespace(telegram_client, db_session):
    update_app_setting(db_session, telegram_chat_id_system=" 111 , 222,  333 ")

    resp_allowed = _post(telegram_client, _payload(222, "/list"))
    assert resp_allowed.status_code == 200
    assert len(telegram_client.sent) == 1

    resp_denied = _post(telegram_client, _payload(444, "/list"))
    assert resp_denied.status_code == 200
    assert len(telegram_client.sent) == 1  # vẫn chỉ 1 - request 444 không được reply


def test_allowlist_string_int_comparison_is_consistent(telegram_client, db_session):
    """Telegram gửi chat.id dạng số (int) trong JSON, còn allowlist lưu string trong
    DB - router phải so sánh nhất quán (str(chat_id)) chứ không so sánh nhầm kiểu
    khiến allowlist đúng số nhưng vẫn bị từ chối oan."""
    update_app_setting(db_session, telegram_chat_id_system="-1001234567890")

    resp = _post(telegram_client, _payload(-1001234567890, "/list"))

    assert resp.status_code == 200
    assert len(telegram_client.sent) == 1


def test_allowlisted_chat_id_can_use_status_filtered_command(telegram_client, db_session):
    update_app_setting(db_session, telegram_chat_id_system="123456789")

    resp = _post(telegram_client, _payload(123456789, "/listdone"))

    assert resp.status_code == 200
    assert len(telegram_client.sent) == 1
    assert "task status Done" in telegram_client.sent[0]["message"]


def test_non_command_text_is_ignored_without_touching_db(telegram_client, db_session):
    """Text không bắt đầu bằng '/' bị bỏ qua sớm (trước cả bước mở DB session) -
    không phải lỗ hổng nhưng xác nhận hành vi hiện tại không đổi khi thêm allowlist."""
    update_app_setting(db_session, telegram_chat_id_system="123456789")

    resp = _post(telegram_client, _payload(123456789, "xin chao"))

    assert resp.status_code == 200
    assert telegram_client.sent == []
