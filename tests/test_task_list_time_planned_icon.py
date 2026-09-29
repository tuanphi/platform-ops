"""Kiem thu icon dong ho + font-size nho hon cho ".time-planned" (gio du kien chay,
t.confirmed_run_at) tren trang Danh sach deploy (app/templates/task_list.html):
  - Icon SVG dong ho chi hien khi t.confirmed_run_at co gia tri (khong hien khi la '-').
  - CSS .time-planned dat font-size: 0.9em (app/templates/base.html)."""

from datetime import datetime

from app.models import DeployTask, Role, TaskStatus
from tests.conftest import make_user


def make_task(db_session, status=TaskStatus.TASK, **kwargs):
    defaults = dict(project="core", application="api", commitid="abc1234", status=status, email="owner@example.com")
    defaults.update(kwargs)
    task = DeployTask(**defaults)
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


def test_time_planned_icon_shown_when_confirmed_run_at_set(client_factory, db_session):
    owner = make_user(db_session, email="owner@example.com", role=Role.USER)
    make_task(
        db_session,
        email=owner.email,
        commitid="icn1111",
        status=TaskStatus.APPROVED,
        confirmed_run_at=datetime(2026, 8, 1, 3, 0, 0),  # UTC -> GMT+7 10:00
    )

    client = client_factory(user=owner)
    resp = client.get("/tasks")

    assert resp.status_code == 200
    assert '<span class="time-planned"><svg class="time-planned-icon"' in resp.text
    assert "01/08/2026 10:00</span>" in resp.text


def test_time_planned_icon_hidden_when_confirmed_run_at_none(client_factory, db_session):
    owner = make_user(db_session, email="owner@example.com", role=Role.USER)
    make_task(
        db_session,
        email=owner.email,
        commitid="icn2222",
        status=TaskStatus.TASK,
        confirmed_run_at=None,
    )

    client = client_factory(user=owner)
    resp = client.get("/tasks")

    assert resp.status_code == 200
    assert '<span class="time-planned">-</span>' in resp.text
    assert '<span class="time-planned"><svg class="time-planned-icon"' not in resp.text


def test_time_planned_css_font_size_smaller(client_factory, db_session):
    owner = make_user(db_session, email="owner@example.com", role=Role.USER)
    client = client_factory(user=owner)
    resp = client.get("/tasks")

    assert resp.status_code == 200
    assert ".time-planned { color: #3699FF; white-space: nowrap; font-size: 0.9em; }" in resp.text
