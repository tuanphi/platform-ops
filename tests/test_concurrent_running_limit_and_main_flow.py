"""Test luồng chính (main flow) cho 4 yêu cầu người dùng liên quan tới bug rate-limit
Run/Rollback + setting mới `max_concurrent_running_tasks` (xem
tests/test_run_rollback_rate_limit.py cho các case rate-limit chi tiết hơn):

1. Run nhiều task khác nhau liên tiếp - không còn bị 429 oan.
2. Run 2 lần cùng 1 task - lần 2 bị từ chối đúng bản chất ("không thể Run").
3. Rollback cùng 1 task trong 3s vẫn bị chặn; Rollback 2 task khác nhau thì không.
4. Setting `max_concurrent_running_tasks` - giới hạn số task Running cùng lúc, 0 = không
   giới hạn.

Convention theo tests/test_run_rollback_rate_limit.py: monkeypatch
task_service.git_service.perform_run/perform_rollback, dùng client_factory + make_user
của conftest, tránh chạm git/mạng thật."""

from __future__ import annotations

from datetime import datetime

from app.models import DeployTask, Role, TaskStatus
from app.services.app_setting_service import update_app_setting
from tests.conftest import make_user


def make_task(db_session, status=TaskStatus.TASK, **kwargs):
    defaults = dict(project="core", application="api", commitid="abc1234", status=status, email="owner@example.com")
    defaults.update(kwargs)
    task = DeployTask(**defaults)
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


# ---------------------------------------------------------------------------
# 1. Run nhiều task khác nhau liên tiếp - không bị chặn
# ---------------------------------------------------------------------------


def test_run_two_different_tasks_immediately_both_succeed(client_factory, db_session, monkeypatch):
    """THAY ĐỔI HÀNH VI CÓ CHỦ Ý (hàng đợi chạy nền - chính giải pháp cho bug production
    gốc): bấm Run giờ CHỈ đưa task vào QUEUED ngay lập tức, KHÔNG còn gọi git đồng bộ trên
    đường request - 2 Run liên tiếp trên 2 task khác nhau không còn tranh chấp git lock gì
    cả (vì không ai gọi git ngay), nên luôn "thành công" theo nghĩa vào hàng đợi."""
    admin = make_user(db_session, email="mf-run@example.com", role=Role.ADMIN)
    task1 = make_task(db_session, status=TaskStatus.APPROVED, commitid="mf00001")
    task2 = make_task(db_session, status=TaskStatus.APPROVED, commitid="mf00002", application="worker")

    from app.services import task_service

    calls = []
    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: (calls.append(1) or task_service.git_service.RunResult(ok=True, message="ok", image_version="v1")),
    )

    client = client_factory(user=admin)
    resp1 = client.post(f"/tasks/{task1.id}/run", data={}, headers={"Accept": "application/json"})
    resp2 = client.post(f"/tasks/{task2.id}/run", data={}, headers={"Accept": "application/json"})

    assert resp1.status_code == 200 and resp1.json()["ok"] is True
    assert resp2.status_code == 200 and resp2.json()["ok"] is True
    db_session.refresh(task1)
    db_session.refresh(task2)
    assert task1.status == TaskStatus.QUEUED
    assert task2.status == TaskStatus.QUEUED
    assert calls == [], "Run KHÔNG được gọi git đồng bộ trên đường request nữa"

    # Kịch bản gốc của bug (xem tests/test_task_queue_worker.py cho bộ test đầy đủ hơn):
    # worker chạy hàng đợi FIFO -> CẢ 2 task đều phải thành công, không task nào lỗi vì
    # tranh chấp git lock.
    results = task_service.process_task_queue(db_session)
    assert len(results) == 2
    assert all(result.ok for _, result in results)
    db_session.refresh(task1)
    db_session.refresh(task2)
    assert task1.status == TaskStatus.RUNNING
    assert task2.status == TaskStatus.RUNNING
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# 2. Run 2 lần cùng 1 task - lần 2 bị từ chối đúng bản chất trạng thái
# ---------------------------------------------------------------------------


def test_run_same_task_twice_second_call_rejected_with_correct_state_message(client_factory, db_session, monkeypatch):
    admin = make_user(db_session, email="mf-run2@example.com", role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.APPROVED, commitid="mf00003")

    from app.services import task_service

    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: task_service.git_service.RunResult(ok=True, message="ok", image_version="v1"),
    )

    client = client_factory(user=admin)
    resp1 = client.post(f"/tasks/{task.id}/run", data={}, headers={"Accept": "application/json"})
    resp2 = client.post(f"/tasks/{task.id}/run", data={}, headers={"Accept": "application/json"})

    assert resp1.status_code == 200 and resp1.json()["ok"] is True
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["ok"] is False
    assert "không thể Run" in body2["message"]
    assert "quá nhanh" not in body2["message"], "Không được nhầm lẫn với message rate-limit cũ đã bị bỏ ở Run"


# ---------------------------------------------------------------------------
# 3. Rollback cùng 1 task trong 3s vẫn bị chặn; Rollback 2 task khác nhau thì không
# ---------------------------------------------------------------------------


def test_rollback_same_task_twice_within_3s_still_blocked_but_different_tasks_are_not(
    client_factory, db_session, monkeypatch
):
    admin = make_user(db_session, email="mf-rollback@example.com", role=Role.ADMIN)
    task_a = make_task(
        db_session, status=TaskStatus.DONE, image_version="v3", commitid="mf00004", done_at=datetime.utcnow()
    )
    target_a1 = make_task(db_session, status=TaskStatus.DONE, image_version="v2", commitid="mf00005")
    target_a2 = make_task(db_session, status=TaskStatus.DONE, image_version="v1", commitid="mf00006")
    task_b = make_task(
        db_session,
        status=TaskStatus.DONE,
        image_version="v2",
        commitid="mf00007",
        application="worker",
        done_at=datetime.utcnow(),
    )
    target_b = make_task(
        db_session, status=TaskStatus.DONE, image_version="v1", commitid="mf00008", application="worker"
    )

    from app.services import task_service

    monkeypatch.setattr(
        task_service.git_service,
        "perform_rollback",
        lambda *a, **k: task_service.git_service.RunResult(ok=True, message="ok", image_version="v1"),
    )

    client = client_factory(user=admin)

    # Rollback lần 1 + lần 2 CÙNG task_a ngay lập tức -> lần 2 vẫn bị 429.
    resp1 = client.post(
        f"/tasks/{task_a.id}/rollback", data={"target_task_id": target_a1.id}, headers={"Accept": "application/json"}
    )
    resp2 = client.post(
        f"/tasks/{task_a.id}/rollback", data={"target_task_id": target_a2.id}, headers={"Accept": "application/json"}
    )
    assert resp1.status_code == 200 and resp1.json()["ok"] is True
    assert resp2.status_code == 429
    assert resp2.json()["ok"] is False

    # Rollback task_b (KHÁC task_a) ngay sau đó -> KHÔNG bị chặn.
    resp3 = client.post(
        f"/tasks/{task_b.id}/rollback", data={"target_task_id": target_b.id}, headers={"Accept": "application/json"}
    )
    assert resp3.status_code == 200, "Rollback task khác không được bị chặn oan"
    assert resp3.json()["ok"] is True


# ---------------------------------------------------------------------------
# 4. Setting max_concurrent_running_tasks: limit=1 chặn task thứ 2, limit=0 = không giới hạn
#
# DỜI TẦNG KIỂM TRA: giới hạn này KHÔNG còn được kiểm tra ở run_task/rollback_task (bấm
# Run/Rollback qua HTTP giờ LUÔN thành công vào hàng đợi, bất kể giới hạn - "kiểm tra sớm
# sẽ vô nghĩa vì task chưa chạy ngay", xem docstring task_service.run_task). Giới hạn giờ
# CHỈ được worker (task_service.process_task_queue, gọi bởi
# app/scheduler.py::job_process_task_queue) áp dụng tại thời điểm THỰC SỰ lấy task ra chạy
# - nên các test dưới đây gọi thẳng process_task_queue() thay vì qua router.
# ---------------------------------------------------------------------------


def test_run_via_http_ignores_concurrent_limit_at_enqueue_time(client_factory, db_session, monkeypatch):
    """Xác nhận rõ ràng phần "dời tầng kiểm tra": bấm Run qua HTTP khi đã đạt giới hạn vẫn
    phải trả ok=True (vào hàng đợi bình thường) - KHÔNG còn bị chặn ngay lúc bấm nút như
    thiết kế cũ (setting này chỉ ảnh hưởng lúc worker lấy task ra chạy)."""
    update_app_setting(db_session, max_concurrent_running_tasks=1)

    admin = make_user(db_session, email="mf-limit-http@example.com", role=Role.ADMIN)
    make_task(db_session, status=TaskStatus.RUNNING, commitid="mf00009")
    task2 = make_task(db_session, status=TaskStatus.APPROVED, commitid="mf00010", application="worker")

    client = client_factory(user=admin)
    resp = client.post(f"/tasks/{task2.id}/run", data={}, headers={"Accept": "application/json"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    db_session.refresh(task2)
    assert task2.status == TaskStatus.QUEUED


def test_concurrent_limit_1_blocks_worker_from_promoting_second_task_while_one_already_running(db_session, monkeypatch):
    update_app_setting(db_session, max_concurrent_running_tasks=1)

    # task1 đã Running sẵn (mô phỏng 1 task khác worker đã lấy ra chạy trước đó).
    make_task(db_session, status=TaskStatus.RUNNING, commitid="mf00009")
    task2 = make_task(
        db_session,
        status=TaskStatus.QUEUED,
        queued_from_status=TaskStatus.APPROVED,
        commitid="mf00010",
        application="worker",
    )

    from app.services import task_service

    calls = []
    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: (calls.append(1) or task_service.git_service.RunResult(ok=True, message="ok", image_version="v1")),
    )

    results = task_service.process_task_queue(db_session)

    assert results == [], "Worker KHÔNG được lấy thêm task nào khi đã đạt giới hạn"
    db_session.refresh(task2)
    assert task2.status == TaskStatus.QUEUED, "Task bị chặn bởi giới hạn phải GIỮ NGUYÊN Queued, không kẹt/không mất"
    assert calls == [], "perform_run (git push) KHÔNG được gọi khi đã bị chặn bởi giới hạn"


def test_concurrent_limit_0_means_unlimited_worker_promotes_even_with_running_task(db_session, monkeypatch):
    update_app_setting(db_session, max_concurrent_running_tasks=0)

    make_task(db_session, status=TaskStatus.RUNNING, commitid="mf00011")
    task2 = make_task(
        db_session,
        status=TaskStatus.QUEUED,
        queued_from_status=TaskStatus.APPROVED,
        commitid="mf00012",
        application="worker",
    )

    from app.services import task_service

    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: task_service.git_service.RunResult(ok=True, message="ok", image_version="v1"),
    )

    results = task_service.process_task_queue(db_session)

    assert len(results) == 1 and results[0][1].ok is True
    db_session.refresh(task2)
    assert task2.status == TaskStatus.RUNNING


def test_concurrent_limit_also_applies_to_rollback(db_session, monkeypatch):
    """Yêu cầu của dev: giới hạn áp dụng cho MỌI đường tạo task Running, kể cả Rollback -
    limit=1 + đã có 1 task Running -> worker KHÔNG được lấy 1 task Rollback QUEUED ra chạy
    (dù nó tới lượt theo FIFO)."""
    update_app_setting(db_session, max_concurrent_running_tasks=1)

    make_task(db_session, status=TaskStatus.RUNNING, commitid="mf00013")
    target = make_task(db_session, status=TaskStatus.DONE, image_version="v1", commitid="mf00015")
    rollback_row = make_task(
        db_session,
        status=TaskStatus.QUEUED,
        image_version="v1",
        rollback_target_task_id=target.id,
        commitid="mf00014",
    )

    from app.services import task_service

    calls = []
    monkeypatch.setattr(
        task_service.git_service,
        "perform_rollback",
        lambda *a, **k: (calls.append(1) or task_service.git_service.RunResult(ok=True, message="ok", image_version="v1")),
    )

    results = task_service.process_task_queue(db_session)

    assert results == []
    db_session.refresh(rollback_row)
    assert rollback_row.status == TaskStatus.QUEUED
    assert calls == [], "perform_rollback (git push) KHÔNG được gọi khi đã bị chặn bởi giới hạn"
