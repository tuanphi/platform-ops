"""Kiểm thử tích hợp router (TestClient) - phân quyền, luồng action, hiển thị theo
quyền. get_db/get_current_user được override sang DB in-memory riêng biệt và
notify_service/scheduler bị vô hiệu hoá (xem tests/conftest.py::client_factory) nên
KHÔNG chạm DB thật hay gọi mạng thật."""

from datetime import datetime, timedelta

from app.models import DeployTask, Group, Role, TaskStatus
from tests.conftest import make_user


def make_group(db_session, name="core"):
    group = Group(group_name=name, is_active=True)
    db_session.add(group)
    db_session.commit()
    db_session.refresh(group)
    return group


def make_task(db_session, status=TaskStatus.TASK, **kwargs):
    defaults = dict(project="core", application="api", commitid="abc1234", status=status, email="owner@example.com")
    defaults.update(kwargs)
    task = DeployTask(**defaults)
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


# ---------------------------------------------------------------------------
# Truy cập trang khi chưa đăng nhập
# ---------------------------------------------------------------------------


def test_unauthenticated_user_redirected_to_login(client_factory):
    client = client_factory(user=None)
    resp = client.get("/tasks", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


# ---------------------------------------------------------------------------
# task_detail - KHÔNG kiểm tra quyền xem, chỉ cần đăng nhập (ghi chú cho security)
# ---------------------------------------------------------------------------


def test_task_detail_blocked_for_unrelated_user_without_view_all_grant(client_factory, db_session):
    """FIXED (B1): GET /tasks/{id} phải chặn user role 'user' bình thường, không liên
    quan gì tới project, xem chi tiết task (bao gồm cả field config_env vốn có thể chứa
    secret) của người khác chỉ bằng cách đổi task_id trên URL - server phải coi như
    "không tìm thấy" (task=None) thay vì trả về nội dung thật."""
    owner = make_user(db_session, email="owner@example.com", role=Role.USER)
    stranger = make_user(db_session, email="stranger@example.com", role=Role.USER)
    task = make_task(
        db_session,
        email=owner.email,
        config_env="DB_PASSWORD=super-secret-value",
    )

    client = client_factory(user=stranger)
    resp = client.get(f"/tasks/{task.id}")

    assert resp.status_code == 200
    assert "super-secret-value" not in resp.text  # secret của owner không bị lộ cho stranger
    assert "Không tìm thấy" in resp.text


def test_task_detail_viewable_by_owner(client_factory, db_session):
    """Chủ sở hữu task vẫn xem được chi tiết task của chính mình."""
    owner = make_user(db_session, email="owner@example.com", role=Role.USER)
    task = make_task(
        db_session,
        email=owner.email,
        config_env="DB_PASSWORD=super-secret-value",
    )

    client = client_factory(user=owner)
    resp = client.get(f"/tasks/{task.id}")

    assert resp.status_code == 200
    assert "super-secret-value" in resp.text
    assert task.project in resp.text


def test_task_detail_viewable_by_admin_regardless_of_ownership(client_factory, db_session):
    """User có can_view_all_tasks (VD: Admin) vẫn xem được chi tiết task của người khác -
    xác nhận fix B1 không chặn nhầm quyền hợp lệ."""
    owner = make_user(db_session, email="owner@example.com", role=Role.USER)
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    task = make_task(
        db_session,
        email=owner.email,
        config_env="DB_PASSWORD=super-secret-value",
    )

    client = client_factory(user=admin)
    resp = client.get(f"/tasks/{task.id}")

    assert resp.status_code == 200
    assert "super-secret-value" in resp.text
    assert task.project in resp.text


def test_task_detail_nonexistent_task_renders_not_found_message(client_factory, db_session):
    user = make_user(db_session, role=Role.USER)
    client = client_factory(user=user)

    resp = client.get("/tasks/999999")

    assert resp.status_code == 200
    assert "Không tìm thấy" in resp.text


# ---------------------------------------------------------------------------
# list_tasks - scope hiển thị theo quyền
# ---------------------------------------------------------------------------


def test_list_tasks_plain_user_only_sees_own_tasks(client_factory, db_session):
    owner = make_user(db_session, email="owner@example.com", role=Role.USER)
    other = make_user(db_session, email="other@example.com", role=Role.USER)
    make_task(db_session, email=owner.email, commitid="aaa1111")
    make_task(db_session, email=other.email, commitid="bbb2222")

    client = client_factory(user=owner)
    resp = client.get("/tasks")

    assert resp.status_code == 200
    assert "aaa1111" in resp.text
    assert "bbb2222" not in resp.text


def test_list_tasks_admin_sees_all_tasks(client_factory, db_session):
    owner = make_user(db_session, email="owner@example.com", role=Role.USER)
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    make_task(db_session, email=owner.email, commitid="aaa1111")

    client = client_factory(user=admin)
    resp = client.get("/tasks")

    assert resp.status_code == 200
    assert "aaa1111" in resp.text


# ---------------------------------------------------------------------------
# action permission enforcement (server-side, không chỉ disabled button trên UI)
# ---------------------------------------------------------------------------


def test_approve_action_denied_for_user_without_grant(client_factory, db_session):
    plain_user = make_user(db_session, email="plain@example.com", role=Role.USER)
    task = make_task(db_session, status=TaskStatus.TASK)

    client = client_factory(user=plain_user)
    resp = client.post(f"/tasks/{task.id}/approve", data={}, headers={"Accept": "application/json"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "không có quyền" in body["message"]
    db_session.refresh(task)
    assert task.status == TaskStatus.TASK  # không đổi trạng thái


def test_approve_action_allowed_for_admin(client_factory, db_session):
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    # confirmed_run_at nay la bat buoc tu Create (feature moi) - mo phong 1 task duoc
    # tao qua luong hien tai, luon co san field nay truoc khi den buoc Approve.
    task = make_task(db_session, status=TaskStatus.TASK, confirmed_run_at=datetime.utcnow() + timedelta(hours=1))

    client = client_factory(user=admin)
    resp = client.post(f"/tasks/{task.id}/approve", data={}, headers={"Accept": "application/json"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    db_session.refresh(task)
    assert task.status == TaskStatus.APPROVED


def test_approve_action_preserves_confirmed_run_at_set_at_create(client_factory, db_session):
    """confirmed_run_at nay duoc set o Create, KHONG con o Approve nua (xem
    task_service.confirm_task) - confirm 1 task khong duoc lam doi gia tri da luu."""
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    run_at = datetime(2026, 8, 1, 3, 0, 0)
    task = make_task(db_session, status=TaskStatus.TASK, confirmed_run_at=run_at)

    client = client_factory(user=admin)
    resp = client.post(f"/tasks/{task.id}/approve", data={}, headers={"Accept": "application/json"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    db_session.refresh(task)
    assert task.confirmed_run_at == run_at


def test_approve_action_on_legacy_task_without_confirmed_run_at_does_not_crash(client_factory, db_session):
    """NGHI VAN LOI CHUC NANG: task cu (tao truoc khi co feature nay, hoac bat ky
    duong nao khien confirmed_run_at con NULL - cot nay van nullable trong models.py)
    can Approve duoc ma khong bi loi 500. Hien tai, actions_router.confirm() dung
    utc_to_gmt7(result.task.confirmed_run_at) de dung f-string noi dung mail MA KHONG
    kiem tra None, va utc_to_gmt7 lam `utc_dt + GMT7_OFFSET` - gay TypeError khi
    confirmed_run_at la None, XAY RA NGAY trong luc xu ly request (f-string duoc
    eval ngay lap tuc, khong phai chi luc background task chay)."""
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.TASK, confirmed_run_at=None)

    client = client_factory(user=admin)
    resp = client.post(f"/tasks/{task.id}/approve", data={}, headers={"Accept": "application/json"})

    assert resp.status_code == 200, (
        "Approve de task com confirmed_run_at=None nao deveria retornar erro de servidor"
    )
    assert resp.json()["ok"] is True


def test_run_action_allowed_for_admin_without_run_grant(client_factory, db_session, monkeypatch):
    """THAY ĐỔI HÀNH VI CÓ CHỦ Ý (yêu cầu chủ dự án): Admin thường (không phải Super
    Admin, không có grant riêng theo Group/Project) giờ ĐƯỢC Run ở tầng server, không
    chỉ ẩn/hiện nút trên UI - trước đây route này trả ok=False cho Admin, giờ phải
    thành công y hệt Super Admin (xem app/models.py::User.can_run). Kết quả thành công
    giờ là QUEUED (hàng đợi chạy nền), không còn RUNNING ngay lập tức - xem
    TaskStatus.QUEUED/task_service.run_task."""
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.APPROVED)

    from app.services import task_service

    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: task_service.git_service.RunResult(ok=True, message="ok", image_version="v1"),
    )

    client = client_factory(user=admin)
    resp = client.post(f"/tasks/{task.id}/run", data={}, headers={"Accept": "application/json"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED


def test_run_action_denied_for_plain_user_without_run_grant(client_factory, db_session):
    """Regression: role USER bình thường, không có grant Group/Project nào, vẫn PHẢI bị
    chặn Run ở tầng server - thay đổi quyền của Admin không được nới lỏng ngoài ý muốn
    sang role USER."""
    plain_user = make_user(db_session, email="plain-run@example.com", role=Role.USER)
    task = make_task(db_session, status=TaskStatus.APPROVED)

    client = client_factory(user=plain_user)
    resp = client.post(f"/tasks/{task.id}/run", data={}, headers={"Accept": "application/json"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    db_session.refresh(task)
    assert task.status == TaskStatus.APPROVED


def test_run_action_allowed_for_admin_directly_from_task_status(client_factory, db_session, monkeypatch):
    """THAY ĐỔI HÀNH VI CÓ CHỦ Ý: Run không còn bắt buộc Approve trước - task status=TASK
    (chưa Approve) phải vào hàng đợi (QUEUED) thành công qua HTTP thật, y hệt task đã
    APPROVED."""
    admin = make_user(db_session, email="admin-direct@example.com", role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.TASK)

    from app.services import task_service

    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: task_service.git_service.RunResult(ok=True, message="ok", image_version="v1"),
    )

    client = client_factory(user=admin)
    resp = client.post(f"/tasks/{task.id}/run", data={}, headers={"Accept": "application/json"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED
    assert task.maintainer_confirmed is None  # Approve bị bỏ qua, không gây lỗi


def test_run_action_allowed_for_super_admin(client_factory, db_session, monkeypatch):
    sa = make_user(db_session, email="sa@example.com", role=Role.SUPER_ADMIN)
    task = make_task(db_session, status=TaskStatus.APPROVED)

    from app.services import task_service

    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: task_service.git_service.RunResult(ok=True, message="ok", image_version="v1"),
    )

    client = client_factory(user=sa)
    resp = client.post(f"/tasks/{task.id}/run", data={}, headers={"Accept": "application/json"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED


# ---------------------------------------------------------------------------
# rollback - dùng chung can_run (xem actions_router.rollback_picker/rollback), cùng
# quy tắc quyền mới của Admin phải áp dụng ở đây (picker còn yêu cầu task.status ==
# "Done" và trong cửa sổ rollback_allowed_seconds, xem models.DeployTask.is_rollback_window_open)
# ---------------------------------------------------------------------------


def test_rollback_picker_allowed_for_admin_without_grant(client_factory, db_session):
    """THAY ĐỔI HÀNH VI CÓ CHỦ Ý: Admin không có grant riêng vẫn mở được trang chọn
    rollback (GET) cho 1 task Done trong cửa sổ cho phép - route dùng chung can_run."""
    admin = make_user(db_session, email="admin-rb@example.com", role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.DONE, image_version="v2", done_at=datetime.utcnow())
    target = make_task(db_session, status=TaskStatus.DONE, image_version="v1", commitid="def5678")

    client = client_factory(user=admin)
    resp = client.get(f"/tasks/{task.id}/rollback")

    assert resp.status_code == 200
    assert "Rollback" in resp.text
    assert target.commitid in resp.text  # picker liệt kê candidate thật, không phải trang từ chối


def test_rollback_picker_denied_for_plain_user_without_grant(client_factory, db_session):
    """Regression: role USER không có grant vẫn KHÔNG mở được trang chọn rollback."""
    plain_user = make_user(db_session, email="plain-rb@example.com", role=Role.USER)
    task = make_task(db_session, status=TaskStatus.DONE, image_version="v2", done_at=datetime.utcnow())
    make_task(db_session, status=TaskStatus.DONE, image_version="v1", commitid="def5678")

    client = client_factory(user=plain_user)
    resp = client.get(f"/tasks/{task.id}/rollback", headers={"Accept": "application/json"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_rollback_action_allowed_for_admin_without_grant(client_factory, db_session, monkeypatch):
    """THAY ĐỔI HÀNH VI CÓ CHỦ Ý: Admin không có grant riêng thực hiện được POST
    rollback thật (tạo task RUNNING mới) - trước đây chỉ Super Admin/user có grant mới
    Rollback được."""
    admin = make_user(db_session, email="admin-rb2@example.com", role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.DONE, image_version="v2", done_at=datetime.utcnow())
    target = make_task(db_session, status=TaskStatus.DONE, image_version="v1", commitid="def5678")

    from app.services import task_service

    monkeypatch.setattr(
        task_service.git_service,
        "perform_rollback",
        lambda *a, **k: task_service.git_service.RunResult(ok=True, message="ok", image_version="v1"),
    )

    client = client_factory(user=admin)
    resp = client.post(
        f"/tasks/{task.id}/rollback",
        data={"target_task_id": target.id},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    db_session.refresh(task)
    assert task.status == TaskStatus.DONE  # task cũ giữ nguyên, rollback tạo task mới


def test_rollback_action_denied_for_plain_user_without_grant(client_factory, db_session):
    """Regression: role USER không có grant vẫn KHÔNG Rollback được qua đường POST."""
    plain_user = make_user(db_session, email="plain-rb2@example.com", role=Role.USER)
    task = make_task(db_session, status=TaskStatus.DONE, image_version="v2", done_at=datetime.utcnow())
    target = make_task(db_session, status=TaskStatus.DONE, image_version="v1", commitid="def5678")

    client = client_factory(user=plain_user)
    resp = client.post(
        f"/tasks/{task.id}/rollback",
        data={"target_task_id": target.id},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    db_session.refresh(task)
    assert task.status == TaskStatus.DONE


# ---------------------------------------------------------------------------
# bulk/run - actions_router._bulk_action dùng cùng can_act=user.can_run
# ---------------------------------------------------------------------------


def test_bulk_run_allowed_for_admin_without_grant(client_factory, db_session, monkeypatch):
    """THAY ĐỔI HÀNH VI CÓ CHỦ Ý: bulk Run (POST /tasks/bulk/run) cũng dùng can_run nên
    Admin không grant phải Run được, không riêng gì route đơn /tasks/{id}/run. Kết quả
    thành công giờ là QUEUED (hàng đợi chạy nền), không còn RUNNING ngay lập tức."""
    admin = make_user(db_session, email="admin-bulk@example.com", role=Role.ADMIN)
    task1 = make_task(db_session, status=TaskStatus.APPROVED, commitid="bulk0001")
    task2 = make_task(db_session, status=TaskStatus.APPROVED, commitid="bulk0002", application="worker")

    from app.services import task_service

    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: task_service.git_service.RunResult(ok=True, message="ok", image_version="v1"),
    )

    client = client_factory(user=admin)
    resp = client.post(
        "/tasks/bulk/run",
        data={"task_ids": [task1.id, task2.id]},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "2/2" in body["message"]
    db_session.refresh(task1)
    db_session.refresh(task2)
    assert task1.status == TaskStatus.QUEUED
    assert task2.status == TaskStatus.QUEUED


def test_bulk_run_mixed_task_confirmed_and_terminal_statuses(client_factory, db_session, monkeypatch):
    """Bulk Run với batch hỗn hợp: task1=TASK (chưa Approve), task2=APPROVED,
    task3=REJECTED (terminal). task1/task2 phải vào hàng đợi thành công (không cần
    Approve trước), task3 phải bị bỏ qua/báo lỗi như cũ, không được Run. bulk/run giờ chỉ
    CAS sang QUEUED (không còn gọi git đồng bộ) nên perform_run KHÔNG được gọi cho BẤT KỲ
    task nào ở đây nữa - khác hành vi cũ (trước đây gọi git ngay cho 2 task hợp lệ)."""
    admin = make_user(db_session, email="admin-mixed@example.com", role=Role.ADMIN)
    task1 = make_task(db_session, status=TaskStatus.TASK, commitid="mix0001")
    task2 = make_task(db_session, status=TaskStatus.APPROVED, commitid="mix0002", application="worker")
    task3 = make_task(db_session, status=TaskStatus.REJECTED, commitid="mix0003", application="scheduler")

    from app.services import task_service

    calls = []
    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: (calls.append(1) or task_service.git_service.RunResult(ok=True, message="ok", image_version="v1")),
    )

    client = client_factory(user=admin)
    resp = client.post(
        "/tasks/bulk/run",
        data={"task_ids": [task1.id, task2.id, task3.id]},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "2/3" in body["message"]
    assert calls == [], "run_task (bulk) KHÔNG được gọi perform_run đồng bộ nữa - đã chuyển sang worker"
    db_session.refresh(task1)
    db_session.refresh(task2)
    db_session.refresh(task3)
    assert task1.status == TaskStatus.QUEUED
    assert task2.status == TaskStatus.QUEUED
    assert task3.status == TaskStatus.REJECTED  # terminal task không bị đổi trạng thái


def test_bulk_run_denied_for_plain_user_without_grant(client_factory, db_session):
    """Regression: bulk Run vẫn chặn role USER không grant cho từng task trong batch."""
    plain_user = make_user(db_session, email="plain-bulk@example.com", role=Role.USER)
    task1 = make_task(db_session, status=TaskStatus.APPROVED, commitid="bulk0003")

    client = client_factory(user=plain_user)
    resp = client.post(
        "/tasks/bulk/run",
        data={"task_ids": [task1.id]},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "0/1" in body["message"]
    db_session.refresh(task1)
    assert task1.status == TaskStatus.APPROVED


def test_cancel_action_allowed_for_task_owner(client_factory, db_session):
    owner = make_user(db_session, email="owner@example.com", role=Role.USER)
    task = make_task(db_session, status=TaskStatus.TASK, email=owner.email)

    client = client_factory(user=owner)
    resp = client.post(f"/tasks/{task.id}/cancel", data={}, headers={"Accept": "application/json"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_cancel_action_denied_for_unrelated_user(client_factory, db_session):
    owner = make_user(db_session, email="owner@example.com", role=Role.USER)
    stranger = make_user(db_session, email="stranger@example.com", role=Role.USER)
    task = make_task(db_session, status=TaskStatus.TASK, email=owner.email)

    client = client_factory(user=stranger)
    resp = client.post(f"/tasks/{task.id}/cancel", data={}, headers={"Accept": "application/json"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False


def test_approve_action_on_nonexistent_task_returns_error_not_500(client_factory, db_session):
    admin = make_user(db_session, role=Role.ADMIN)
    client = client_factory(user=admin)

    resp = client.post("/tasks/999999/approve", data={}, headers={"Accept": "application/json"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is False


# ---------------------------------------------------------------------------
# Server-side re-check trên action bị vi phạm vòng đời (xem thêm test_task_service_lifecycle.py)
# ---------------------------------------------------------------------------


def test_run_action_blocks_running_already_rejected_task_via_http(client_factory, db_session, monkeypatch):
    """FIXED (A1): qua đường HTTP thật, user có quyền Run (super_admin) KHÔNG được Run
    1 task đã bị Reject trước đó - state machine phải chặn ở tầng service, không chỉ ở
    tầng quyền hạn."""
    sa = make_user(db_session, email="sa@example.com", role=Role.SUPER_ADMIN)
    task = make_task(db_session, status=TaskStatus.REJECTED)

    from app.services import task_service

    calls = []
    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: (calls.append(1) or task_service.git_service.RunResult(ok=True, message="ok", image_version="v1")),
    )

    client = client_factory(user=sa)
    resp = client.post(f"/tasks/{task.id}/run", data={}, headers={"Accept": "application/json"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert len(calls) == 0  # git push không được gọi
    db_session.refresh(task)
    assert task.status == TaskStatus.REJECTED  # trạng thái không đổi


# ---------------------------------------------------------------------------
# /users, /catalog - chỉ Super Admin
# ---------------------------------------------------------------------------


def test_users_page_forbidden_for_admin_role(client_factory, db_session):
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    client = client_factory(user=admin)

    resp = client.get("/users")

    assert resp.status_code == 403


def test_users_page_allowed_for_super_admin(client_factory, db_session):
    sa = make_user(db_session, email="sa@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)

    resp = client.get("/users")

    assert resp.status_code == 200


def test_catalog_page_forbidden_for_plain_user(client_factory, db_session):
    user = make_user(db_session, role=Role.USER)
    client = client_factory(user=user)

    resp = client.get("/catalog")

    assert resp.status_code == 403


def test_catalog_page_still_forbidden_for_admin_role(client_factory, db_session):
    """Regression ranh giới MUỐN GIỮ NGUYÊN: /catalog chỉ Super Admin (require_settings_
    access -> can_manage_settings) truy cập được - việc mở rộng can_run cho Admin ở
    models.py KHÔNG được ảnh hưởng tới phạm vi quản trị cấu hình/catalog này."""
    admin = make_user(db_session, email="admin-catalog@example.com", role=Role.ADMIN)
    client = client_factory(user=admin)

    resp = client.get("/catalog")

    assert resp.status_code == 403


def test_set_user_role_rejects_invalid_role_value(client_factory, db_session):
    sa = make_user(db_session, email="sa@example.com", role=Role.SUPER_ADMIN)
    target = make_user(db_session, email="target@example.com", role=Role.USER)
    client = client_factory(user=sa)

    resp = client.post(
        f"/users/{target.id}/role", data={"role": "super-hacker"}, headers={"Accept": "application/json"}
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    db_session.refresh(target)
    assert target.role == Role.USER


def test_set_user_password_rejects_short_password(client_factory, db_session):
    sa = make_user(db_session, email="sa@example.com", role=Role.SUPER_ADMIN)
    target = make_user(db_session, email="target@example.com", role=Role.USER)
    client = client_factory(user=sa)

    resp = client.post(
        f"/users/{target.id}/password", data={"password": "123"}, headers={"Accept": "application/json"}
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is False


# ---------------------------------------------------------------------------
# Regression: POST /tasks/{id}/auto-deploy (bat/tat Auto deploy) - hoan toan tach
# biet voi confirmed_run_at (chi la gio nhac nho, khong tu dong Run), xac nhan
# thay doi field confirmed_run_at o Create khong lam anh huong toi luong nay.
# ---------------------------------------------------------------------------


def test_auto_deploy_enable_allowed_for_admin(client_factory, db_session):
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.APPROVED, confirmed_run_at=datetime.utcnow())

    client = client_factory(user=admin)
    resp = client.post(
        f"/tasks/{task.id}/auto-deploy",
        data={"enabled": "1", "auto_run_at": "2026-08-01T10:00"},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    db_session.refresh(task)
    assert task.auto_deploy is True
    assert task.auto_run_at == datetime(2026, 8, 1, 3, 0, 0)
    # confirmed_run_at (gio nhac nho, nhap tu Create) khong bi Auto deploy dung/doi
    assert task.confirmed_run_at is not None


def test_auto_deploy_enable_requires_run_at(client_factory, db_session):
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.APPROVED, confirmed_run_at=datetime.utcnow())

    client = client_factory(user=admin)
    resp = client.post(
        f"/tasks/{task.id}/auto-deploy",
        data={"enabled": "1", "auto_run_at": ""},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    db_session.refresh(task)
    assert task.auto_deploy is False


def test_auto_deploy_disable_allowed_for_admin(client_factory, db_session):
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    task = make_task(
        db_session,
        status=TaskStatus.APPROVED,
        confirmed_run_at=datetime.utcnow(),
        auto_deploy=True,
        auto_run_at=datetime.utcnow() + timedelta(hours=1),
    )

    client = client_factory(user=admin)
    resp = client.post(
        f"/tasks/{task.id}/auto-deploy",
        data={"enabled": "0", "auto_run_at": ""},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    db_session.refresh(task)
    assert task.auto_deploy is False
    assert task.auto_run_at is None


def test_auto_deploy_denied_for_plain_user_without_grant(client_factory, db_session):
    plain_user = make_user(db_session, email="plain@example.com", role=Role.USER)
    task = make_task(db_session, status=TaskStatus.APPROVED, confirmed_run_at=datetime.utcnow())

    client = client_factory(user=plain_user)
    resp = client.post(
        f"/tasks/{task.id}/auto-deploy",
        data={"enabled": "1", "auto_run_at": "2026-08-01T10:00"},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    db_session.refresh(task)


# ---------------------------------------------------------------------------
# bulk/delete - chi Super Admin, chi task terminal (Rejected/Cancelled/Done)
# ---------------------------------------------------------------------------


def test_bulk_delete_allowed_for_super_admin_on_terminal_tasks(client_factory, db_session):
    super_admin = make_user(db_session, email="super-delete@example.com", role=Role.SUPER_ADMIN)
    task1 = make_task(db_session, status=TaskStatus.REJECTED, commitid="del0001")
    task2 = make_task(db_session, status=TaskStatus.DONE, commitid="del0002", application="worker")
    task1_id, task2_id = task1.id, task2.id

    client = client_factory(user=super_admin)
    resp = client.post(
        "/tasks/bulk/delete",
        data={"task_ids": [task1_id, task2_id]},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "2/2" in body["message"]
    assert db_session.get(DeployTask, task1_id) is None
    assert db_session.get(DeployTask, task2_id) is None


def test_bulk_delete_denied_for_admin_role_not_super_admin(client_factory, db_session):
    """Admin (khong phai Super Admin) khong duoc Xoa - can_delete_task chi True cho
    Role.SUPER_ADMIN, khac han can_approve/can_run von cho ca Admin."""
    admin = make_user(db_session, email="admin-delete@example.com", role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.DONE, commitid="del0003")

    client = client_factory(user=admin)
    resp = client.post(
        "/tasks/bulk/delete",
        data={"task_ids": [task.id]},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert db_session.get(DeployTask, task.id) is not None


def test_bulk_delete_rejects_non_terminal_status(client_factory, db_session):
    """Super Admin van khong xoa duoc task chua ket thuc (Task/Approved/Running) - tranh
    mat du lieu dang xu ly."""
    super_admin = make_user(db_session, email="super-delete2@example.com", role=Role.SUPER_ADMIN)
    task = make_task(db_session, status=TaskStatus.APPROVED, commitid="del0004")

    client = client_factory(user=super_admin)
    resp = client.post(
        "/tasks/bulk/delete",
        data={"task_ids": [task.id]},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "0/1" in body["message"]
    assert db_session.get(DeployTask, task.id) is not None
    assert task.auto_deploy is False
