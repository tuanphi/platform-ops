"""SSE endpoint để trang /catalog, /staging, /production tự nhận biết "vừa có sync mới"
(do git_webhook_router trigger) và tự quay nút Sync/Làm mới + reload, xem
app/services/sync_events.py.

Chỉ yêu cầu đăng nhập (không phân theo quyền can_access_staging/production/settings) -
payload đẩy qua đây chỉ là 1 chuỗi nguồn ("catalog"/"staging"/"production"), không chứa dữ
liệu nhạy cảm; quyền xem trang thật vẫn do chính route GET của từng trang kiểm soát."""

import asyncio

from fastapi import APIRouter, Depends, Request
from starlette.responses import StreamingResponse

from app.auth import get_current_user
from app.models import User
from app.services import sync_events

router = APIRouter(tags=["sync-events"])

# Gui 1 dong keep-alive (comment SSE, khong phai event that) sau moi khoang thoi gian nay
# neu khong co sync nao xay ra - giu ket noi song, tranh bi proxy/trinh duyet tu dong coi
# la timeout va dong ket noi.
_KEEPALIVE_SECONDS = 15


@router.get("/events/sync")
async def sync_events_stream(request: Request, user: User = Depends(get_current_user)):
    queue = sync_events.subscribe()

    async def event_stream():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_SECONDS)
                    yield f"event: sync\ndata: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            sync_events.unsubscribe(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
