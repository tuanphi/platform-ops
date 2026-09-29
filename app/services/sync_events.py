"""Broadcaster nội bộ (in-process, KHÔNG dùng Redis/message broker ngoài) đẩy sự kiện
"vừa đồng bộ xong" qua Server-Sent Events tới các trang đang mở (/catalog, /staging,
/production) - dùng để tự động quay nút Sync/Làm mới khi git_webhook_router vừa sync
xong do nhận webhook, không cần người dùng tự bấm/tự reload mới thấy.

CHỈ hoạt động đúng khi app chạy 1 worker process duy nhất (uvicorn mặc định, không kèm
--workers hay gunicorn -w N) - mỗi worker có bộ subscriber (asyncio.Queue) riêng trong bộ
nhớ của chính nó, request webhook rơi vào worker nào chỉ đẩy được tới các tab đang giữ kết
nối SSE với ĐÚNG worker đó."""

import asyncio
import json

_subscribers: set[asyncio.Queue] = set()


def subscribe() -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue()
    _subscribers.add(queue)
    return queue


def unsubscribe(queue: asyncio.Queue) -> None:
    _subscribers.discard(queue)


def publish(source: str) -> None:
    """source: "catalog" | "staging" | "production" - gọi từ git_webhook_router SAU khi 1
    sync thành công. put_nowait (không chờ) - 1 subscriber chậm/queue đầy không được làm
    chậm response webhook; hàng đợi đầy (client không đọc kịp) thì bỏ qua sự kiện đó cho
    subscriber đó, không quan trọng bằng việc không chặn caller."""
    payload = json.dumps({"source": source})
    for queue in list(_subscribers):
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass
