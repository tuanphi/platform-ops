"""Kiểm thử app/routers/sync_events_router.py (GET /events/sync).

KHÔNG test qua TestClient.stream() thật - generator vô hạn (chờ tin nhắn/keep-alive 15s)
khiến request treo, TestClient không có cách ngắt kết nối ASGI giữa chừng một cách đáng
tin cậy trong test đồng bộ. Test phần xác thực (trả lời TRƯỚC khi bắt đầu stream, nên
nhanh/an toàn) qua HTTP thật; test nội dung generator bằng cách gọi thẳng hàm async, tự
kiểm soát số lần pull thay vì để nó chạy vô hạn."""

import asyncio
import json

from fastapi.testclient import TestClient

from app.routers import sync_events_router


class _FakeRequest:
    async def is_disconnected(self) -> bool:
        return False


def test_requires_login(monkeypatch):
    import app.main as main_module

    monkeypatch.setattr(main_module, "start_scheduler", lambda: None)
    monkeypatch.setattr(main_module, "shutdown_scheduler", lambda: None)

    client = TestClient(main_module.app)
    res = client.get("/events/sync", follow_redirects=False)

    assert res.status_code in (302, 303, 401, 403)


def test_event_stream_yields_formatted_sse_message_and_subscribes(monkeypatch):
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait(json.dumps({"source": "staging"}))

    subscribed = []
    monkeypatch.setattr(sync_events_router.sync_events, "subscribe", lambda: (subscribed.append(1), queue)[1])

    async def _run():
        response = await sync_events_router.sync_events_stream(_FakeRequest(), user=None)
        assert response.media_type == "text/event-stream"
        chunk = await response.body_iterator.__anext__()
        return chunk

    chunk = asyncio.run(_run())

    assert subscribed == [1]
    assert chunk == 'event: sync\ndata: {"source": "staging"}\n\n'
