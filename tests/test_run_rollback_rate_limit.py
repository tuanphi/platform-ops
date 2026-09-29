"""Kiểm thử bản vá lỗ hổng MEDIUM (DoS) mới nhất trên `app/routers/actions_router.py` +
`app/services/git_service.py`:

1. Rate limit per-user (in-process dict, khoá theo email) cho Run/Rollback/bulk-Run:
   - 2 lần gọi liên tiếp CÙNG 1 user trong cửa sổ cooldown -> lần 2 bị chặn 429.
   - Sau khi cooldown trôi qua -> gọi lại thành công.
   - 2 user KHÁC NHAU không chặn nhầm nhau.
   - Check đặt SAU kiểm tra quyền `can_run` -> bị từ chối vì thiếu quyền KHÔNG tốn slot
     cooldown.
2. Fail-fast git lock (`_GIT_LOCK_TIMEOUT_SECONDS` 90s -> 2s): khi lock đang bị giữ, xin
   lock lần nữa phải fail nhanh (~2s, KHÔNG chờ tới 90s) với GitError chứa "đang bận".

CHỈ THÊM TEST MỚI - không sửa/xoá bất kỳ test case nào sẵn có ở nơi khác. File này không
đụng tới mã nguồn app, chỉ thêm test."""

from __future__ import annotations

import time
from datetime import datetime

import pytest
from filelock import FileLock

from app.models import DeployTask, Role, TaskStatus
from app.services import git_service
from tests.conftest import make_user


def make_group(db_session, name="core"):
    from app.models import Group

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
# 1. Rate limit per-user cho Run
# ---------------------------------------------------------------------------


def test_run_two_different_tasks_back_to_back_are_never_rate_limited(client_factory, db_session, monkeypatch):
    """Hành vi MỚI (thay cho cooldown theo email đã bị bỏ ở Run/bulk Run): Run 2 task
    KHÁC NHAU liên tiếp ngay lập tức (cùng user) - cả 2 phải thành công, KHÔNG còn bị 429
    (bug thật production: Run task #34 xong, vài giây sau Run task #35 bị chặn oan vì
    dùng chung cooldown theo email). Từ khi có hàng đợi chạy nền, "thành công" nghĩa là
    QUEUED ngay lập tức (không còn gọi git đồng bộ) - đây chính là bản vá triệt để hơn cho
    đúng bug gốc: 2 Run gần nhau không còn tranh chấp git lock nữa vì không ai gọi git
    ngay trên đường request cả."""
    admin = make_user(db_session, email="rl-admin@example.com", role=Role.ADMIN)
    task1 = make_task(db_session, status=TaskStatus.APPROVED, commitid="rl00001")
    task2 = make_task(db_session, status=TaskStatus.APPROVED, commitid="rl00002", application="worker")

    from app.services import task_service

    calls = []
    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: (calls.append(1) or task_service.git_service.RunResult(ok=True, message="ok", image_version="v1")),
    )

    client = client_factory(user=admin)

    resp1 = client.post(f"/tasks/{task1.id}/run", data={}, headers={"Accept": "application/json"})
    assert resp1.status_code == 200
    assert resp1.json()["ok"] is True

    # Gọi Run lần 2 NGAY LẬP TỨC (cùng user, task KHÁC) - không còn cooldown chung theo
    # email nên KHÔNG được chặn.
    resp2 = client.post(f"/tasks/{task2.id}/run", data={}, headers={"Accept": "application/json"})
    assert resp2.status_code == 200, "Run task khác ngay sau đó KHÔNG được bị chặn 429 oan"
    body2 = resp2.json()
    assert body2["ok"] is True

    db_session.refresh(task1)
    db_session.refresh(task2)
    assert task1.status == TaskStatus.QUEUED
    assert task2.status == TaskStatus.QUEUED, "Task2 (task khác, gọi ngay sau task1) phải vào hàng đợi thành công"
    assert calls == [], "run_task KHÔNG được gọi perform_run (git push) đồng bộ nữa - đã chuyển sang worker"


def test_run_twice_same_task_second_call_rejected_with_wrong_state_message_not_rate_limit(
    client_factory, db_session, monkeypatch
):
    """Run 2 lần liên tiếp CÙNG 1 task: lần 2 vẫn phải bị chặn (task đã RUNNING không thể
    Run lại), nhưng bằng message đúng bản chất "không thể Run" (task_service.CONFLICT/
    trạng thái không hợp lệ) - KHÔNG còn phải là message rate-limit "thao tác quá nhanh"
    (đã bị bỏ ở Run)."""
    admin = make_user(db_session, email="rl-admin-same@example.com", role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.APPROVED, commitid="rlsame01")

    from app.services import task_service

    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: task_service.git_service.RunResult(ok=True, message="ok", image_version="v1"),
    )

    client = client_factory(user=admin)

    resp1 = client.post(f"/tasks/{task.id}/run", data={}, headers={"Accept": "application/json"})
    assert resp1.status_code == 200 and resp1.json()["ok"] is True

    resp2 = client.post(f"/tasks/{task.id}/run", data={}, headers={"Accept": "application/json"})
    assert resp2.status_code == 200, "Không còn là 429 rate-limit - đây là từ chối logic nghiệp vụ (status code 200 theo convention hiện có của app cho các lỗi nghiệp vụ khác)"
    body2 = resp2.json()
    assert body2["ok"] is False
    assert "không thể Run" in body2["message"], f"Message phải phản ánh đúng bản chất trạng thái task, thực tế: {body2['message']!r}"
    assert "quá nhanh" not in body2["message"]

    db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED, "Task vẫn ở trạng thái QUEUED từ lần Run đầu tiên"


def test_rollback_second_call_same_task_within_cooldown_is_blocked_429(client_factory, db_session, monkeypatch):
    """Cooldown Rollback vẫn được GIỮ LẠI (khác Run) nhưng nay khoá theo (email, task_id)
    thay vì email: Rollback 2 LẦN LIÊN TIẾP trên CÙNG 1 task nguồn vẫn phải bị chặn 429 -
    rollback_task() không có CAS bảo vệ (chỉ INSERT dòng mới), nên vẫn cần cooldown chặn
    spam trên cùng 1 task."""
    admin = make_user(db_session, email="rl-admin-rb@example.com", role=Role.ADMIN)
    task_a = make_task(
        db_session, status=TaskStatus.DONE, image_version="v3", commitid="rlrb0001", done_at=datetime.utcnow()
    )
    target_v2 = make_task(db_session, status=TaskStatus.DONE, image_version="v2", commitid="rlrb0002")
    target_v1 = make_task(db_session, status=TaskStatus.DONE, image_version="v1", commitid="rlrb0005")

    from app.services import task_service

    monkeypatch.setattr(
        task_service.git_service,
        "perform_rollback",
        lambda *a, **k: task_service.git_service.RunResult(ok=True, message="ok", image_version="v1"),
    )

    client = client_factory(user=admin)

    resp1 = client.post(
        f"/tasks/{task_a.id}/rollback",
        data={"target_task_id": target_v2.id},
        headers={"Accept": "application/json"},
    )
    assert resp1.status_code == 200
    assert resp1.json()["ok"] is True

    # Rollback lần 2 NGAY LẬP TỨC trên CÙNG task_a (target khác cũng không quan trọng, bị
    # chặn TRƯỚC khi chạm tới task_service) - vẫn phải bị 429.
    resp2 = client.post(
        f"/tasks/{task_a.id}/rollback",
        data={"target_task_id": target_v1.id},
        headers={"Accept": "application/json"},
    )
    assert resp2.status_code == 429, "Rollback lần 2 cùng task trong cửa sổ cooldown phải bị chặn HTTP 429"
    assert resp2.json()["ok"] is False

    db_session.refresh(task_a)
    assert task_a.status == TaskStatus.DONE  # rollback tạo task mới, task cũ giữ nguyên


def test_rollback_two_different_tasks_back_to_back_are_not_rate_limited(client_factory, db_session, monkeypatch):
    """Hành vi MỚI của khoá (email, task_id): Rollback 2 task NGUỒN KHÁC NHAU liên tiếp
    (cùng user) KHÔNG được chặn nhau - trước đây (khoá theo email) sẽ bị chặn oan giống
    hệt bug Run task khác nhau đã báo cáo trong production."""
    admin = make_user(db_session, email="rl-shared@example.com", role=Role.ADMIN)
    rb_task_a = make_task(
        db_session,
        status=TaskStatus.DONE,
        image_version="v2",
        commitid="rlsh0002",
        done_at=datetime.utcnow(),
    )
    rb_target_a = make_task(db_session, status=TaskStatus.DONE, image_version="v1", commitid="rlsh0003")
    rb_task_b = make_task(
        db_session,
        status=TaskStatus.DONE,
        image_version="v2",
        commitid="rlsh0004",
        application="worker",
        done_at=datetime.utcnow(),
    )
    rb_target_b = make_task(
        db_session, status=TaskStatus.DONE, image_version="v1", commitid="rlsh0005", application="worker"
    )

    from app.services import task_service

    monkeypatch.setattr(
        task_service.git_service,
        "perform_rollback",
        lambda *a, **k: task_service.git_service.RunResult(ok=True, message="ok", image_version="v1"),
    )

    client = client_factory(user=admin)

    resp1 = client.post(
        f"/tasks/{rb_task_a.id}/rollback",
        data={"target_task_id": rb_target_a.id},
        headers={"Accept": "application/json"},
    )
    assert resp1.status_code == 200 and resp1.json()["ok"] is True

    resp2 = client.post(
        f"/tasks/{rb_task_b.id}/rollback",
        data={"target_task_id": rb_target_b.id},
        headers={"Accept": "application/json"},
    )
    assert resp2.status_code == 200, "Rollback task KHÁC ngay sau đó KHÔNG được bị chặn 429 oan"
    assert resp2.json()["ok"] is True


def test_run_succeeds_again_after_cooldown_elapses(client_factory, db_session, monkeypatch):
    """Sau khi cooldown trôi qua (mô phỏng bằng cách lùi mốc thời gian đã ghi nhận trong
    dict state), gọi lại Run phải thành công bình thường, không còn bị 429."""
    admin = make_user(db_session, email="rl-elapsed@example.com", role=Role.ADMIN)
    task1 = make_task(db_session, status=TaskStatus.APPROVED, commitid="rle0001")
    task2 = make_task(db_session, status=TaskStatus.APPROVED, commitid="rle0002", application="worker")

    from app.services import task_service

    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: task_service.git_service.RunResult(ok=True, message="ok", image_version="v1"),
    )

    client = client_factory(user=admin)

    resp1 = client.post(f"/tasks/{task1.id}/run", data={}, headers={"Accept": "application/json"})
    assert resp1.status_code == 200 and resp1.json()["ok"] is True

    # Giả lập thời gian đã trôi qua quá cooldown: lùi mốc last-call đã ghi nhận trong
    # dict state (module-level) của actions_router thay vì sleep thật trong test.
    import app.routers.actions_router as actions_router_module

    cooldown = actions_router_module._RUN_ROLLBACK_COOLDOWN_SECONDS
    actions_router_module._run_rollback_last_call[admin.email] = time.monotonic() - (cooldown + 1)

    resp2 = client.post(f"/tasks/{task2.id}/run", data={}, headers={"Accept": "application/json"})
    assert resp2.status_code == 200, "Sau khi cooldown trôi qua, Run phải thành công lại (không còn 429)"
    assert resp2.json()["ok"] is True

    db_session.refresh(task2)
    assert task2.status == TaskStatus.QUEUED


def test_two_different_users_running_concurrently_do_not_block_each_other(client_factory, db_session, monkeypatch):
    """Rate limit PER-USER (theo email) - 2 user khác nhau gọi Run liên tiếp/gần như đồng
    thời KHÔNG được chặn nhầm nhau."""
    admin1 = make_user(db_session, email="rl-user-a@example.com", role=Role.ADMIN)
    admin2 = make_user(db_session, email="rl-user-b@example.com", role=Role.ADMIN)
    task1 = make_task(db_session, status=TaskStatus.APPROVED, commitid="rlu0001")
    task2 = make_task(db_session, status=TaskStatus.APPROVED, commitid="rlu0002", application="worker")

    from app.services import task_service

    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: task_service.git_service.RunResult(ok=True, message="ok", image_version="v1"),
    )

    client1 = client_factory(user=admin1)
    resp1 = client1.post(f"/tasks/{task1.id}/run", data={}, headers={"Accept": "application/json"})
    assert resp1.status_code == 200 and resp1.json()["ok"] is True

    # user2 gọi Run NGAY SAU đó (cùng tiến trình, dict state module-level dùng chung) -
    # không được bị ảnh hưởng bởi cooldown của user1.
    client2 = client_factory(user=admin2)
    resp2 = client2.post(f"/tasks/{task2.id}/run", data={}, headers={"Accept": "application/json"})
    assert resp2.status_code == 200, "User khác gọi Run ngay sau đó KHÔNG được bị chặn oan bởi cooldown của user1"
    assert resp2.json()["ok"] is True

    db_session.refresh(task1)
    db_session.refresh(task2)
    assert task1.status == TaskStatus.QUEUED
    assert task2.status == TaskStatus.QUEUED


def test_rate_limit_check_is_after_permission_check_denied_does_not_consume_cooldown(
    client_factory, db_session, monkeypatch
):
    """Check rate limit nằm SAU kiểm tra quyền can_run: user thiếu quyền bị từ chối vì
    quyền (KHÔNG phải vì rate limit), và lần bị từ chối đó KHÔNG được tính vào cooldown -
    sau khi được cấp quyền, gọi lại NGAY LẬP TỨC (không cần chờ) phải KHÔNG bị 429."""
    plain_user = make_user(db_session, email="rl-perm@example.com", role=Role.USER)
    task = make_task(db_session, status=TaskStatus.APPROVED, commitid="rlp0001")

    from app.services import task_service

    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: task_service.git_service.RunResult(ok=True, message="ok", image_version="v1"),
    )

    client = client_factory(user=plain_user)

    resp_denied = client.post(f"/tasks/{task.id}/run", data={}, headers={"Accept": "application/json"})
    assert resp_denied.status_code == 200, "Từ chối vì thiếu quyền không phải 429 (không phải do rate limit)"
    body_denied = resp_denied.json()
    assert body_denied["ok"] is False
    assert "quyền" in body_denied["message"]

    db_session.refresh(task)
    assert task.status == TaskStatus.APPROVED  # chưa hề bị Run

    # Cấp quyền Run ngay (nâng role) rồi gọi lại NGAY LẬP TỨC, không sleep - nếu slot
    # cooldown đã lỡ bị tiêu tốn bởi lần bị từ chối trước đó thì request này sẽ bị 429 oan.
    plain_user.role = Role.ADMIN
    db_session.commit()

    resp_allowed = client.post(f"/tasks/{task.id}/run", data={}, headers={"Accept": "application/json"})
    assert resp_allowed.status_code == 200, "Lần gọi lại sau khi được cấp quyền không được bị 429 oan"
    assert resp_allowed.json()["ok"] is True

    db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED


def test_single_normal_run_call_is_never_rate_limited(client_factory, db_session, monkeypatch):
    """Happy path: 1 lần gọi Run bình thường (không lặp lại) không bao giờ bị 429 - dict
    state rỗng ban đầu (được conftest reset trước mỗi test)."""
    admin = make_user(db_session, email="rl-single@example.com", role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.APPROVED, commitid="rls0001")

    from app.services import task_service

    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: task_service.git_service.RunResult(ok=True, message="ok", image_version="v1"),
    )

    client = client_factory(user=admin)
    resp = client.post(f"/tasks/{task.id}/run", data={}, headers={"Accept": "application/json"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_run_twice_same_task_blocks_via_normal_form_redirect_with_session_flash(client_factory, db_session, monkeypatch):
    """Đường form thường (không phải AJAX) khi Run 2 lần cùng 1 task: redirect 303 tới URL
    SẠCH (không còn querystring ok=0/msg=..., Phase 3 đã bỏ) - lỗi "không thể Run" (task đã
    QUEUED từ lần Run đầu tiên, ok=False) phải tới được người dùng qua session flash khi GET
    theo location đó (không phải raise lỗi 500/khác), và KHÔNG còn phải là message
    rate-limit cũ."""
    import json

    admin = make_user(db_session, email="rl-form@example.com", role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.APPROVED, commitid="rlf0001")

    from app.services import task_service

    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: task_service.git_service.RunResult(ok=True, message="ok", image_version="v1"),
    )

    client = client_factory(user=admin)
    resp1 = client.post(f"/tasks/{task.id}/run", data={}, follow_redirects=False)
    assert resp1.status_code == 303

    resp2 = client.post(f"/tasks/{task.id}/run", data={}, follow_redirects=False)
    assert resp2.status_code == 303
    location = resp2.headers["location"]
    assert location == f"/tasks/{task.id}"
    assert "ok=" not in location
    assert "msg=" not in location

    # Loi "khong the Run" (ok=False) van phai toi duoc nguoi dung qua session flash, KHONG
    # con la _RATE_LIMIT_MESSAGE (rate limit da bo o Run).
    from app.routers.actions_router import _RATE_LIMIT_MESSAGE

    follow_get = client.get(location)
    assert follow_get.status_code == 200
    assert json.dumps(_RATE_LIMIT_MESSAGE) not in follow_get.text
    expected_message = f"Task đang ở trạng thái {TaskStatus.QUEUED.value}, không thể Run!"
    assert json.dumps(expected_message) in follow_get.text


def test_rollback_rate_limit_blocks_via_normal_form_redirect_with_session_flash(client_factory, db_session, monkeypatch):
    """Rollback vẫn giữ cooldown (khoá theo (email, task_id)) - đường form thường khi bị
    chặn: redirect 303 tới URL sạch, message rate-limit gốc (_RATE_LIMIT_MESSAGE) phải tới
    được người dùng qua session flash."""
    import json

    admin = make_user(db_session, email="rl-rb-form@example.com", role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.DONE, image_version="v2", commitid="rlfrb01", done_at=datetime.utcnow())
    target1 = make_task(db_session, status=TaskStatus.DONE, image_version="v1", commitid="rlfrb02")
    target2 = make_task(db_session, status=TaskStatus.DONE, image_version="v0", commitid="rlfrb03")

    from app.services import task_service

    monkeypatch.setattr(
        task_service.git_service,
        "perform_rollback",
        lambda *a, **k: task_service.git_service.RunResult(ok=True, message="ok", image_version="v1"),
    )

    client = client_factory(user=admin)
    resp1 = client.post(f"/tasks/{task.id}/rollback", data={"target_task_id": target1.id}, follow_redirects=False)
    assert resp1.status_code == 303

    resp2 = client.post(f"/tasks/{task.id}/rollback", data={"target_task_id": target2.id}, follow_redirects=False)
    assert resp2.status_code == 303
    location = resp2.headers["location"]
    assert location == f"/tasks/{task.id}"
    assert "ok=" not in location
    assert "msg=" not in location

    from app.routers.actions_router import _RATE_LIMIT_MESSAGE

    follow_get = client.get(location)
    assert follow_get.status_code == 200
    assert json.dumps(_RATE_LIMIT_MESSAGE) in follow_get.text


# ---------------------------------------------------------------------------
# 2. Fail-fast git lock (_GIT_LOCK_TIMEOUT_SECONDS 90s -> 2s)
# ---------------------------------------------------------------------------


def test_git_lock_default_timeout_constant_is_fail_fast_not_90s():
    """Khẳng định trực tiếp giá trị hằng số đã đổi - guard against regression vô tình đổi
    lại 90s (hoặc 1 giá trị quá lớn nào khác) trong tương lai."""
    assert git_service._GIT_LOCK_TIMEOUT_SECONDS <= 5, (
        "_GIT_LOCK_TIMEOUT_SECONDS phải là fail-fast (vài giây), không được quay lại giá "
        "trị cũ 90s - nếu không request Run/Rollback tranh chấp sẽ lại giữ thread threadpool "
        "quá lâu (lỗ hổng DoS ban đầu)"
    )
    assert git_service._GIT_LOCK_TIMEOUT_BACKGROUND_SECONDS > git_service._GIT_LOCK_TIMEOUT_SECONDS


def test_master_repo_lock_fails_fast_within_a_few_seconds_when_held(tmp_path, monkeypatch):
    """master_repo_lock() (dùng cho Run/Rollback) phải fail nhanh (~2s, dùng
    _GIT_LOCK_TIMEOUT_SECONDS thật của module, KHÔNG monkeypatch xuống số nhỏ hơn để đo
    đúng hành vi thực tế sau bản vá) khi lock đang bị giữ bởi tiến trình khác - KHÔNG được
    chờ tới 90s như bản vá cũ."""
    monkeypatch.setattr(git_service.settings, "git_directory_applications_master", str(tmp_path / "checkout-master"))

    lock_path = git_service._lock_file_path(str(tmp_path / "checkout-master"))
    holder = FileLock(str(lock_path))
    holder.acquire()
    try:
        started = time.monotonic()
        with pytest.raises(git_service.GitError, match="đang bận"):
            with git_service.master_repo_lock():
                pytest.fail("Không được vào critical section khi chưa lấy được lock")
        elapsed = time.monotonic() - started
        assert elapsed < 10, f"master_repo_lock() phải fail-fast trong vài giây, thực tế mất {elapsed:.1f}s"
    finally:
        holder.release()


def test_staging_repo_lock_default_is_fail_fast_background_true_waits_longer(tmp_path, monkeypatch):
    """staging_repo_lock(background=False) (mặc định, dùng cho check_commit_on_staging
    trên đường đi của request người dùng) phải dùng timeout NGẮN
    (_GIT_LOCK_TIMEOUT_SECONDS); staging_repo_lock(background=True) (catalog sync) phải
    dùng timeout DÀI HƠN (_GIT_LOCK_TIMEOUT_BACKGROUND_SECONDS) - test bằng cách
    monkeypatch 2 hằng số xuống giá trị nhỏ khác biệt rõ rệt để đo mà không phải chờ thật
    sự lâu."""
    monkeypatch.setattr(git_service, "_GIT_LOCK_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(git_service, "_GIT_LOCK_TIMEOUT_BACKGROUND_SECONDS", 1.0)
    monkeypatch.setattr(git_service.settings, "git_directory_applications_staging", str(tmp_path / "checkout-staging"))

    lock_path = git_service._lock_file_path(str(tmp_path / "checkout-staging"))
    holder = FileLock(str(lock_path))
    holder.acquire()
    try:
        started = time.monotonic()
        with pytest.raises(git_service.GitError, match="đang bận"):
            with git_service.staging_repo_lock(background=False):
                pytest.fail("Không được vào critical section (fail-fast) khi chưa lấy được lock")
        elapsed_fail_fast = time.monotonic() - started

        started = time.monotonic()
        with pytest.raises(git_service.GitError, match="đang bận"):
            with git_service.staging_repo_lock(background=True):
                pytest.fail("Không được vào critical section (background) khi chưa lấy được lock")
        elapsed_background = time.monotonic() - started
    finally:
        holder.release()

    assert elapsed_fail_fast < 0.6, f"staging_repo_lock(background=False) phải fail-fast, thực tế {elapsed_fail_fast:.2f}s"
    assert elapsed_background >= 0.9, (
        f"staging_repo_lock(background=True) phải chờ theo _GIT_LOCK_TIMEOUT_BACKGROUND_SECONDS, "
        f"thực tế chỉ {elapsed_background:.2f}s (có vẻ vẫn đang dùng timeout ngắn của fail-fast)"
    )
    assert elapsed_background > elapsed_fail_fast


def test_catalog_sync_uses_background_lock_not_fail_fast(monkeypatch):
    """sync_catalog_from_chart_repo phải gọi staging_repo_lock(background=True) - nếu Dev
    lỡ quên tham số này (hoặc revert về mặc định fail-fast), job đồng bộ catalog nền có
    thể fail vô cớ ngay khi có 1 request Create/Run khác đang verify commit trên staging
    cùng lúc (tranh chấp bình thường, không nên coi là lỗi)."""
    from types import SimpleNamespace

    from app.services import catalog_service

    captured = {}

    def fake_staging_repo_lock(background=False):
        captured["background"] = background
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            yield

        return _cm()

    monkeypatch.setattr(catalog_service, "staging_repo_lock", fake_staging_repo_lock)
    monkeypatch.setattr(catalog_service, "ensure_staging_repo", lambda repo_url: "/tmp/fake-staging-does-not-exist")
    monkeypatch.setattr(
        catalog_service,
        "get_app_setting",
        lambda db: SimpleNamespace(
            catalog_excluded_dirs="",
            git_remote_repo_url="https://example.com/repo.git",
            catalog_charts_dir="charts",
        ),
    )

    try:
        catalog_service.sync_catalog_from_chart_repo(None)
    except Exception:
        # Không quan tâm phần logic sync còn lại thành công hay không (đã có test riêng
        # cho catalog_service) - chỉ cần xác nhận staging_repo_lock được gọi với
        # background=True trước khi có exception nào khác xảy ra (vd charts_dir giả
        # không tồn tại -> FileNotFoundError, xảy ra SAU khi lock đã được xin đúng cách).
        pass

    assert captured.get("background") is True, "sync_catalog_from_chart_repo phải dùng staging_repo_lock(background=True)"
