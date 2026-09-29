"""Kiểm thử app/services/notify_service.py chịu lỗi mạng (Telegram/SMTP down) không được
làm crash luồng chính (send_telegram/send_mail luôn nuốt lỗi, chỉ log)."""

import httpx
import pytest

from app.services import notify_service


def test_send_telegram_swallows_network_error(monkeypatch):
    monkeypatch.setattr(notify_service.settings, "telegram_bot_token", "fake-token")
    monkeypatch.setattr(notify_service, "get_app_setting", lambda db=None: type("S", (), {"telegram_chat_id_system": "123"})())

    def fake_post(*a, **k):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(notify_service.httpx, "post", fake_post)

    # Không được raise exception ra ngoài
    notify_service.send_telegram("hello")


def test_send_telegram_noop_when_token_missing(monkeypatch):
    monkeypatch.setattr(notify_service.settings, "telegram_bot_token", "")
    calls = []
    monkeypatch.setattr(notify_service.httpx, "post", lambda *a, **k: calls.append(1))

    notify_service.send_telegram("hello")

    assert calls == []


def test_send_mail_swallows_smtp_error(monkeypatch):
    fake_setting = type(
        "S",
        (),
        {
            "enable_mail": True,
            "mail_to": "dev@example.com",
            "mail_from": "form-deploy@example.com",
            "mail_subject": "subject",
        },
    )()
    monkeypatch.setattr(notify_service, "get_app_setting", lambda db=None: fake_setting)
    monkeypatch.setattr(notify_service.settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(notify_service.settings, "smtp_user", "")

    class FakeSMTP:
        def __init__(self, *a, **k):
            raise OSError("connection refused")

    monkeypatch.setattr(notify_service.smtplib, "SMTP", FakeSMTP)

    # Không được raise exception ra ngoài
    notify_service.send_mail("body")


def test_build_ascii_table_handles_empty_rows():
    assert notify_service.build_ascii_table([]) == "Không có gì"


def test_build_ascii_table_renders_rows():
    table = notify_service.build_ascii_table([{"A": 1, "B": "x"}])
    assert "<pre>" in table
    assert "A" in table and "B" in table


def test_build_ascii_table_escapes_html_in_cell_values():
    """Fix bao mat: gia tri o (vd task.email nguoi dung nhap) phai duoc html.escape truoc
    khi nhung vao <pre> - message nay duoc gui voi parse_mode=HTML (Telegram) hoac nhung
    vao email HTML, the <a>/<script> chua duoc escape se bi render that."""
    table = notify_service.build_ascii_table([{"Mail Request": "<a href='http://evil.example'>click</a>"}])
    assert "<a href=" not in table
    assert "&lt;a href=" in table
