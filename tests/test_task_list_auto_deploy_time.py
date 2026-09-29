"""HAPPY PATH cho hien thi gio hen Auto deploy o cot "Auto" (task_list.html) - truoc day
gio hen (`auto_run_at`) CHI hien qua tooltip `title` khi hover, gio hien truc tiep bang
1 dong nho duoi/canh nut gat (xem .auto-deploy-cell/.auto-deploy-time trong base.html)."""

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


def test_auto_deploy_on_shows_scheduled_time(client_factory, db_session):
    """TC1: task auto_deploy=True + co auto_run_at -> hien span .auto-deploy-time voi gio
    da doi sang GMT+7 (auto_run_at luu UTC, filter gmt7 cong 7 tieng), dinh dang hh:mm dd/mm."""
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    make_task(
        db_session,
        auto_deploy=True,
        auto_run_at=datetime(2026, 8, 1, 3, 0, 0),  # UTC 03:00 -> GMT+7 10:00
    )

    client = client_factory(user=admin)
    resp = client.get("/tasks")

    assert resp.status_code == 200
    assert 'class="auto-deploy-time"' in resp.text
    assert "01/08/2026 10:00" in resp.text


def test_auto_deploy_off_hides_scheduled_time(client_factory, db_session):
    """TC2: task auto_deploy=False -> KHONG hien span gio hen (chi con nut gat), du co the
    con auto_run_at cu trong DB tu lan bat truoc do (khong dc ro ri gio "cu" sau khi tat)."""
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    make_task(db_session, auto_deploy=False, auto_run_at=None)

    client = client_factory(user=admin)
    resp = client.get("/tasks")

    assert resp.status_code == 200
    assert 'class="auto-deploy-time"' not in resp.text
