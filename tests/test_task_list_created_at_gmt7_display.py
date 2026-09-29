"""HAPPY PATH: cot "Thời gian" > span.time-created tren trang /tasks phai hien created_at
da doi sang GMT+7 (truoc day thieu filter `gmt7`, hien thang gio UTC gay lech 7 tieng voi
nguoi dung VN). Fix: app/templates/task_list.html:266 doi tu `{{ t.created_at | dmy }}`
thanh `{{ t.created_at | gmt7 | dmy }}` (dang ky filter tai app/routers/tasks_router.py).
Test theo cung convention voi test_task_list_auto_deploy_time.py: dung client_factory +
db_session that, render nguyen trang /tasks qua router that (khong mock jinja rieng)."""

from datetime import datetime

from app.models import DeployTask, Role, TaskStatus
from tests.conftest import make_user


def make_task(db_session, **kwargs):
    defaults = dict(
        project="core",
        application="api",
        commitid="c000001",
        status=TaskStatus.TASK,
        email="admin@example.com",
    )
    defaults.update(kwargs)
    task = DeployTask(**defaults)
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


def test_time_created_shows_created_at_shifted_to_gmt7(client_factory, db_session):
    """TC1: created_at luu UTC 02:30 -> span.time-created phai hien 09:30 (UTC+7), khong
    phai 02:30 (UTC tho, bug truoc khi fix)."""
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    make_task(
        db_session,
        created_at=datetime(2026, 8, 6, 2, 30, 0),  # UTC 02:30 -> GMT+7 09:30
    )

    client = client_factory(user=admin)
    resp = client.get("/tasks")

    assert resp.status_code == 200
    assert 'class="time-created"' in resp.text
    assert "06/08/2026 09:30" in resp.text
    assert "06/08/2026 02:30" not in resp.text


def test_time_created_shifts_across_midnight_into_next_day(client_factory, db_session):
    """TC2: cong 7 tieng lam sang ngay hom sau -> ngay hien thi phai tang theo dung, khong
    chi cong gio ma giu nguyen ngay cu."""
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    make_task(
        db_session,
        created_at=datetime(2026, 8, 5, 20, 0, 0),  # UTC 05/08 20:00 -> GMT+7 06/08 03:00
    )

    client = client_factory(user=admin)
    resp = client.get("/tasks")

    assert resp.status_code == 200
    assert "06/08/2026 03:00" in resp.text
    assert "05/08/2026 20:00" not in resp.text
