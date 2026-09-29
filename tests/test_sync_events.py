"""Kiểm thử app/services/sync_events.py - broadcaster in-process (asyncio.Queue) đẩy sự
kiện "vừa sync xong" qua SSE tới các trang /catalog, /staging, /production."""

import asyncio
import json

from app.services import sync_events


def test_publish_delivers_to_subscribed_queue():
    queue = sync_events.subscribe()
    try:
        sync_events.publish("staging")
        payload = queue.get_nowait()
        assert json.loads(payload) == {"source": "staging"}
    finally:
        sync_events.unsubscribe(queue)


def test_publish_delivers_to_multiple_subscribers():
    q1 = sync_events.subscribe()
    q2 = sync_events.subscribe()
    try:
        sync_events.publish("catalog")
        assert json.loads(q1.get_nowait()) == {"source": "catalog"}
        assert json.loads(q2.get_nowait()) == {"source": "catalog"}
    finally:
        sync_events.unsubscribe(q1)
        sync_events.unsubscribe(q2)


def test_unsubscribe_stops_delivery():
    queue = sync_events.subscribe()
    sync_events.unsubscribe(queue)

    sync_events.publish("production")

    assert queue.empty()


def test_publish_with_no_subscribers_does_not_raise():
    sync_events.publish("catalog")  # khong co subscriber nao - khong duoc raise


def test_publish_does_not_block_when_a_subscriber_queue_is_full():
    queue = asyncio.Queue(maxsize=1)
    sync_events._subscribers.add(queue)
    try:
        sync_events.publish("staging")  # dien day queue (maxsize=1)
        sync_events.publish("staging")  # queue day - phai bo qua, khong raise/block
        assert queue.qsize() == 1
    finally:
        sync_events.unsubscribe(queue)
