"""Kiểm thử hiển thị webUI sau khi đổi toàn bộ datetime sang dd/mm/yyyy HH:MM (filter
`dmy`, xem app/timeutil.py::format_dmy + đăng ký trong tasks_router.py/actions_router.py):
  - task_detail.html: Giờ nhắc nhở/Auto deploy (GMT+7 + dmy), Run at/Done at (gmt7 + dmy).
  - task_list.html: tooltip Auto deploy (GMT+7 + dmy), cột "Thời gian" (created_at dmy +
    confirmed_run_at gmt7/dmy, đổi tên từ "Tạo lúc").
  - rollback_picker.html: cột "Chạy lúc" (gmt7 + dmy).

Trọng tâm: (1) giá trị None -> '-' không phải 'None'/lỗi 500, (2) không còn dạng
yyyy-mm-dd trong HTML, (3) KHÔNG có hồi quy timezone - toàn bộ datetime lưu UTC trong DB
(confirmed_run_at/auto_run_at/run_at/done_at) đều hiển thị thống nhất theo GMT+7 (+7h)
qua filter chain `gmt7 | dmy`."""

import re
from datetime import datetime

from app.models import DeployTask, Group, Project, Role, TaskStatus
from tests.conftest import make_user

YMD_PATTERN = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def make_group(db_session, name="core"):
    group = Group(group_name=name, is_active=True)
    db_session.add(group)
    db_session.commit()
    db_session.refresh(group)
    return group


def make_project(db_session, group, application="api", is_active=True):
    project = Project(group_id=group.id, application_name=application, is_active=is_active)
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)
    return project


def make_task(db_session, status=TaskStatus.TASK, **kwargs):
    defaults = dict(project="core", application="api", commitid="abc1234", status=status, email="owner@example.com")
    defaults.update(kwargs)
    task = DeployTask(**defaults)
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


# ---------------------------------------------------------------------------
# task_detail.html
# ---------------------------------------------------------------------------


def test_task_detail_renders_dmy_format_for_all_datetime_fields(client_factory, db_session):
    owner = make_user(db_session, email="owner@example.com", role=Role.USER)
    task = make_task(
        db_session,
        email=owner.email,
        status=TaskStatus.DONE,
        confirmed_run_at=datetime(2026, 8, 1, 3, 0, 0),  # UTC luu DB, GMT+7 = 10:00
        run_at=datetime(2026, 8, 1, 3, 5, 0),  # UTC luu DB, GMT+7 = 10:05
        done_at=datetime(2026, 8, 1, 3, 10, 0),  # UTC luu DB, GMT+7 = 10:10
    )

    client = client_factory(user=owner)
    resp = client.get(f"/tasks/{task.id}")

    assert resp.status_code == 200
    # confirmed_run_at: UTC 03:00 -> GMT+7 10:00 -> dmy "01/08/2026 10:00"
    assert "01/08/2026 10:00" in resp.text
    # run_at/done_at: cung doi qua gmt7 (+7h) nhu cac truong khac -> dmy tren gia tri
    # GMT+7 10:05 / 10:10
    assert "01/08/2026 10:05" in resp.text
    assert "01/08/2026 10:10" in resp.text
    # Khong con dinh dang yyyy-mm-dd nao sot lai trong HTML
    assert not YMD_PATTERN.search(resp.text), f"Con sot dinh dang yyyy-mm-dd: {YMD_PATTERN.findall(resp.text)}"


def test_task_detail_null_datetime_fields_render_dash_not_none_not_500(client_factory, db_session):
    owner = make_user(db_session, email="owner@example.com", role=Role.USER)
    task = make_task(
        db_session,
        email=owner.email,
        status=TaskStatus.TASK,
        confirmed_run_at=None,
        run_at=None,
        done_at=None,
    )

    client = client_factory(user=owner)
    resp = client.get(f"/tasks/{task.id}")

    assert resp.status_code == 200
    assert "None" not in resp.text
    # 3 dong Gio nhac nho / Run at / Done at deu phai fallback ve '-'
    assert resp.text.count("<td>-</td>") >= 3


def test_task_detail_auto_deploy_on_shows_dmy_gmt7(client_factory, db_session):
    owner = make_user(db_session, email="owner@example.com", role=Role.USER)
    task = make_task(
        db_session,
        email=owner.email,
        status=TaskStatus.APPROVED,
        auto_deploy=True,
        auto_run_at=datetime(2026, 8, 1, 3, 0, 0),  # UTC -> GMT+7 10:00
    )

    client = client_factory(user=owner)
    resp = client.get(f"/tasks/{task.id}")

    assert resp.status_code == 200
    assert "Bật — 01/08/2026 10:00 (giờ VN)" in resp.text


# ---------------------------------------------------------------------------
# task_list.html
# ---------------------------------------------------------------------------


def test_task_list_created_at_column_renders_dmy(client_factory, db_session):
    """Cot "Thoi gian" (doi ten tu "Tao luc"): dong 1 luon la created_at (gmt7 + dmy), dong 2 la
    confirmed_run_at (gmt7 + dmy) hoac "-" neu chua dat - xem .time-cell trong base.html.
    created_at duoc luu UTC nhung hien thi da doi sang GMT+7 (UTC 14:30 -> 21:30)."""
    owner = make_user(db_session, email="owner@example.com", role=Role.USER)
    make_task(
        db_session,
        email=owner.email,
        commitid="fff6666",
        created_at=datetime(2026, 7, 29, 14, 30, 0),
        confirmed_run_at=None,
    )

    client = client_factory(user=owner)
    resp = client.get("/tasks")

    assert resp.status_code == 200
    assert '<td data-label="Thời gian">' in resp.text
    assert '<span class="time-created">29/07/2026 21:30</span>' in resp.text
    assert '<span class="time-planned">-</span>' in resp.text
    assert not YMD_PATTERN.search(resp.text), f"Con sot dinh dang yyyy-mm-dd: {YMD_PATTERN.findall(resp.text)}"


def test_task_list_confirmed_run_at_shows_dmy_gmt7_in_time_cell(client_factory, db_session):
    """Task da co confirmed_run_at (vd sau khi Approve) -> dong 2 hien gio da doi GMT+7,
    dinh dang dmy, KHONG con la '-'."""
    owner = make_user(db_session, email="owner@example.com", role=Role.USER)
    make_task(
        db_session,
        email=owner.email,
        commitid="aaa1111",
        status=TaskStatus.APPROVED,
        confirmed_run_at=datetime(2026, 8, 1, 3, 0, 0),  # UTC -> GMT+7 10:00
    )

    client = client_factory(user=owner)
    resp = client.get("/tasks")

    assert resp.status_code == 200
    # Co icon dong ho (chi hien khi confirmed_run_at co gia tri) + van giu dung dinh dang dmy
    assert 'class="time-planned-icon"' in resp.text
    assert '<span class="time-planned"><svg class="time-planned-icon"' in resp.text
    assert "01/08/2026 10:00</span>" in resp.text


def test_task_list_auto_deploy_tooltip_renders_dmy_gmt7(client_factory, db_session):
    owner = make_user(db_session, email="owner@example.com", role=Role.USER)
    make_task(
        db_session,
        email=owner.email,
        commitid="ggg7777",
        status=TaskStatus.APPROVED,
        auto_deploy=True,
        auto_run_at=datetime(2026, 8, 1, 3, 0, 0),
    )

    client = client_factory(user=owner)
    resp = client.get("/tasks")

    assert resp.status_code == 200
    assert "Auto lúc 01/08/2026 10:00 (giờ VN)" in resp.text


def test_task_list_auto_deploy_off_tooltip_no_datetime_leak(client_factory, db_session):
    owner = make_user(db_session, email="owner@example.com", role=Role.USER)
    make_task(db_session, email=owner.email, commitid="hhh8888", auto_deploy=False, auto_run_at=None)

    client = client_factory(user=owner)
    resp = client.get("/tasks")

    assert resp.status_code == 200
    assert "Bật Auto deploy" in resp.text


# ---------------------------------------------------------------------------
# rollback_picker.html
# ---------------------------------------------------------------------------


def test_rollback_picker_run_at_column_renders_dmy(client_factory, db_session):
    owner = make_user(db_session, email="owner@example.com", role=Role.SUPER_ADMIN)
    current = make_task(
        db_session,
        email=owner.email,
        commitid="iii9999",
        status=TaskStatus.DONE,
        image_version="v2",
        done_at=datetime.utcnow(),
    )
    candidate = make_task(
        db_session,
        email=owner.email,
        commitid="jjj0000",
        status=TaskStatus.DONE,
        image_version="v1",
        run_at=datetime(2026, 8, 1, 3, 5, 0),  # UTC luu DB, GMT+7 = 10:05
        done_at=datetime.utcnow(),
    )

    client = client_factory(user=owner)
    resp = client.get(f"/tasks/{current.id}/rollback")

    assert resp.status_code == 200
    assert "01/08/2026 10:05" in resp.text
    assert str(candidate.id) in resp.text
    assert not YMD_PATTERN.search(resp.text), f"Con sot dinh dang yyyy-mm-dd: {YMD_PATTERN.findall(resp.text)}"


def test_rollback_picker_null_run_at_renders_dash_not_none(client_factory, db_session):
    owner = make_user(db_session, email="owner@example.com", role=Role.SUPER_ADMIN)
    current = make_task(
        db_session,
        email=owner.email,
        commitid="kkk1111",
        status=TaskStatus.DONE,
        image_version="v2",
        done_at=datetime.utcnow(),
    )
    make_task(
        db_session,
        email=owner.email,
        commitid="lll2222",
        status=TaskStatus.DONE,
        image_version="v1",
        run_at=None,
        done_at=datetime.utcnow(),
    )

    client = client_factory(user=owner)
    resp = client.get(f"/tasks/{current.id}/rollback")

    assert resp.status_code == 200
    assert "None" not in resp.text
    assert '<td data-label="Chạy lúc">-</td>' in resp.text


# ---------------------------------------------------------------------------
# E2E: Create -> Approve -> Run va luong Auto deploy qua HTTP that su (khong chi set
# truc tiep DB), dung dung dinh dang ISO ma JS parseDmyDatetime() se gui, dam bao doi
# input confirmed_run_at/auto_run_at sang type="text" khong lam vo luong nghiep vu, va
# task_detail render dung dmy o tung buoc.
# ---------------------------------------------------------------------------


def test_e2e_create_approve_run_flow_renders_dmy_correctly(client_factory, db_session, monkeypatch):
    from app.services import task_service

    user = make_user(db_session, email="dev@example.com", role=Role.SUPER_ADMIN)
    group = make_group(db_session)
    make_project(db_session, group, application="api")
    monkeypatch.setattr(
        task_service.git_service,
        "check_commit_on_staging",
        lambda *a, **k: task_service.git_service.CommitCheckResult(status="ok", message="ok", image_version="v1"),
    )

    client = client_factory(user=user)

    # 1) Create - confirmed_run_at gui dung dinh dang ISO ma JS parseDmyDatetime() tao ra
    # tu chuoi "01/08/2026 10:00" nguoi dung go (gio VN).
    create_resp = client.post(
        "/tasks/create",
        data={
            "group_id": group.id,
            "application": "api",
            "commitid": "e2e1234",
            "config_env": "",
            "updatefor": "e2e test",
            "confirmed_run_at": "2026-08-01T10:00",
        },
        follow_redirects=False,
    )
    assert create_resp.status_code == 303
    task_id = int(create_resp.headers["location"].split("/tasks/")[1].split("?")[0])

    detail_after_create = client.get(f"/tasks/{task_id}")
    assert detail_after_create.status_code == 200
    assert "01/08/2026 10:00" in detail_after_create.text  # Gio nhac nho hien dung dmy+GMT7

    # 2) Approve
    approve_resp = client.post(f"/tasks/{task_id}/approve", data={}, headers={"Accept": "application/json"})
    assert approve_resp.status_code == 200
    assert approve_resp.json()["ok"] is True

    # 3) Run - CHỈ đưa vào hàng đợi (QUEUED) ngay lập tức, KHÔNG gọi git đồng bộ nữa (xem
    # TaskStatus.QUEUED/task_service.run_task) - trang detail lúc này vẫn phải render đúng
    # dmy, không phải 'None'/yyyy-mm-dd, dù run_at còn None.
    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: task_service.git_service.RunResult(ok=True, message="ok", image_version="v2"),
    )
    run_resp = client.post(f"/tasks/{task_id}/run", data={}, headers={"Accept": "application/json"})
    assert run_resp.status_code == 200
    assert run_resp.json()["ok"] is True

    task = db_session.get(DeployTask, task_id)
    db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED
    assert task.run_at is None

    detail_after_queue = client.get(f"/tasks/{task_id}")
    assert detail_after_queue.status_code == 200
    assert "None" not in detail_after_queue.text
    assert not YMD_PATTERN.search(detail_after_queue.text)

    # 4) Worker (job_process_task_queue) lấy task ra chạy thật - sau bước này task mới
    # thực sự Running với run_at đã set, trang detail vẫn phải render đúng dmy.
    task_service.process_task_queue(db_session)

    db_session.refresh(task)
    assert task.status == TaskStatus.RUNNING
    assert task.run_at is not None

    detail_after_run = client.get(f"/tasks/{task_id}")
    assert detail_after_run.status_code == 200
    assert "None" not in detail_after_run.text
    assert not YMD_PATTERN.search(detail_after_run.text)


def test_e2e_auto_deploy_enable_then_task_list_and_detail_render_dmy(client_factory, db_session):
    admin = make_user(db_session, email="admin2@example.com", role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.APPROVED, confirmed_run_at=datetime.utcnow(), email=admin.email)

    client = client_factory(user=admin)

    enable_resp = client.post(
        f"/tasks/{task.id}/auto-deploy",
        data={"enabled": "1", "auto_run_at": "2026-08-01T10:00"},
        headers={"Accept": "application/json"},
    )
    assert enable_resp.status_code == 200
    assert enable_resp.json()["ok"] is True

    list_resp = client.get("/tasks")
    assert list_resp.status_code == 200
    assert "Auto lúc 01/08/2026 10:00 (giờ VN)" in list_resp.text

    detail_resp = client.get(f"/tasks/{task.id}")
    assert detail_resp.status_code == 200
    assert "Bật — 01/08/2026 10:00 (giờ VN)" in detail_resp.text

    disable_resp = client.post(
        f"/tasks/{task.id}/auto-deploy",
        data={"enabled": "0", "auto_run_at": ""},
        headers={"Accept": "application/json"},
    )
    assert disable_resp.status_code == 200
    assert disable_resp.json()["ok"] is True

    detail_after_disable = client.get(f"/tasks/{task.id}")
    assert detail_after_disable.status_code == 200
    assert "Tắt" in detail_after_disable.text
