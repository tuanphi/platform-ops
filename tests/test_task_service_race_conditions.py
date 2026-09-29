"""Kiểm thử race condition cho bản vá CAS (`app/services/task_service._cas_update`) và
file lock git (`app/services/git_service._repo_lock`).

CHỈ THÊM TEST MỚI - không sửa/xoá bất kỳ test case nào sẵn có trong
`tests/test_task_service_lifecycle.py` hay các file test khác.

Hai nhóm test:
1. Test "deterministic" (không dùng thread thật): monkeypatch `_cas_update` để CHỦ ĐỘNG
   tiêm 1 thao tác ghi "đối thủ" (dùng 1 Session khác) ngay TRƯỚC KHI câu UPDATE thật của
   caller đang test chạy - mô phỏng chính xác khoảng hở race condition (đọc status bằng
   Python -> ghi) mà không phụ thuộc vào việc hệ điều hành có thực sự xen kẽ 2 thread hay
   không (tránh flaky). Đây là cách kiểm chứng đáng tin cậy nhất cho tính đúng đắn của CAS.
2. Test dùng THREAD THẬT + `threading.Barrier` trên 1 SQLite file-based engine (mỗi
   thread có Session/connection riêng, khác với fixture `db_session` mặc định của
   conftest vốn dùng 1 connection SQLite in-memory DÙNG CHUNG qua StaticPool - không an
   toàn để mô phỏng 2 connection ghi đồng thời thật sự) - bổ sung bằng chứng ở mức "hành
   vi cuối cùng nhất quán" khi có tranh chấp thật giữa các thread.

LƯU Ý QUAN TRỌNG VỀ MÔI TRƯỜNG TEST (đọc kỹ trước khi đánh giá kết quả):
SQLite (dùng trong toàn bộ suite này, kể cả file-based engine ở đây) LUÔN trả
`cursor.rowcount` = số dòng KHỚP mệnh đề WHERE của UPDATE, bất kể giá trị có thực sự đổi
hay không. Production dùng MySQL qua pymysql - theo đúng ghi chú của Dev trong
`_cas_update`, pymysql mặc định KHÔNG bật `CLIENT_FOUND_ROWS` nên rowcount của MySQL chỉ
đếm dòng THỰC SỰ ĐỔI GIÁ TRỊ. Vì `_cas_update` đã ép luôn `updated_at` (giá trị always-new)
vào mọi lần UPDATE nên về lý thuyết hành vi 2 backend phải giống nhau, nhưng bộ test này
KHÔNG chạy được trên MySQL thật nên KHÔNG có bằng chứng thực nghiệm trên đúng driver/DB
production - xem mục cảnh báo cuối báo cáo bàn giao.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from datetime import datetime, timedelta

import pytest
from filelock import FileLock
from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker

from app.models import Base, DeployTask, Role, TaskStatus, User
from app.services import git_service, task_service
from tests.conftest import make_user
from tests.test_task_service_lifecycle import make_group, make_project, make_task

# ---------------------------------------------------------------------------
# Nhóm 1: mô phỏng race condition có chủ đích (deterministic, không dùng thread thật)
# ---------------------------------------------------------------------------


def _second_session_for(db_session):
    """Tạo 1 Session ORM thứ 2 trỏ vào CÙNG engine/connection với `db_session` - dùng
    để mô phỏng 1 request khác thao tác đồng thời (tuần tự về mặt Python nhưng đại diện
    đúng cho 1 transaction/commit độc lập xen vào giữa lúc caller đang test đọc task và
    lúc CAS UPDATE thật sự chạy)."""
    factory = sessionmaker(bind=db_session.get_bind(), autoflush=False, autocommit=False)
    return factory()


def test_run_loses_to_concurrent_reject_never_pushes_code(db_session, monkeypatch):
    """KỊCH BẢN NGUY HIỂM NHẤT theo yêu cầu: nếu Reject "thắng" trong khoảng hở giữa lúc
    Run đọc status và lúc Run thực sự CAS sang RUNNING, Run PHẢI thất bại và tuyệt đối
    KHÔNG được gọi git_service.perform_run (không được push code cho 1 task đã Rejected)."""
    runner = make_user(db_session, email="runner@example.com", role=Role.SUPER_ADMIN)
    rejecter = make_user(db_session, email="rejecter@example.com", role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.APPROVED)

    perform_run_calls = []
    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: (perform_run_calls.append(1) or task_service.git_service.RunResult(ok=True, message="ok", image_version="v1")),
    )

    original_cas = task_service._cas_update
    injected = {"done": False}

    def cas_with_injected_reject_first(db, task_id, condition, values):
        # Chi tiem 1 lan duy nhat, dung luc Run dang chuan bi CAS "chiem cho" RUNNING -
        # dai dien cho 1 request Reject khac vua commit xong NGAY TRUOC do.
        if not injected["done"]:
            injected["done"] = True
            other_session = _second_session_for(db_session)
            try:
                reject_result = task_service.reject_task(other_session, task_id, rejecter)
                assert reject_result.ok is True, "Setup lỗi: Reject 'đối thủ' phải thành công"
            finally:
                other_session.close()
        return original_cas(db, task_id, condition, values)

    monkeypatch.setattr(task_service, "_cas_update", cas_with_injected_reject_first)

    run_result = task_service.run_task(db_session, task.id, runner)

    assert run_result.ok is False, "Run phải thất bại vì Reject đã thắng trong lúc tranh chấp"
    assert "vừa được thao tác" in run_result.message or "Rejected" in run_result.message
    assert perform_run_calls == [], "NGUY HIỂM: Run đã gọi git push dù Reject đã thắng trước đó"

    verify_session = _second_session_for(db_session)
    try:
        final_task = verify_session.get(DeployTask, task.id)
        assert final_task.status == TaskStatus.REJECTED, "Task không được ở trạng thái nào khác ngoài Rejected"
    finally:
        verify_session.close()


def test_reject_loses_to_concurrent_run_reservation(db_session, monkeypatch):
    """Chiều ngược lại: nếu Run đã "chiếm chỗ" QUEUED trước (từ khi có hàng đợi, Run CHỈ
    CAS sang QUEUED - xem TaskStatus.QUEUED/task_service.run_task, không còn chiếm RUNNING
    ngay trên đường request nữa), Reject đến sau PHẢI vẫn bị chặn (QUEUED không nằm trong
    REJECTABLE_STATUSES - không được để task đã vào hàng đợi vừa bị đánh dấu Rejected)."""
    runner = make_user(db_session, email="runner@example.com", role=Role.SUPER_ADMIN)
    rejecter = make_user(db_session, email="rejecter@example.com", role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.TASK)

    original_cas = task_service._cas_update
    injected = {"done": False}

    def cas_with_injected_run_first(db, task_id, condition, values):
        if not injected["done"]:
            injected["done"] = True
            other_session = _second_session_for(db_session)
            try:
                run_result = task_service.run_task(other_session, task_id, runner)
                assert run_result.ok is True, "Setup lỗi: Run 'đối thủ' phải thành công"
            finally:
                other_session.close()
        return original_cas(db, task_id, condition, values)

    monkeypatch.setattr(task_service, "_cas_update", cas_with_injected_run_first)

    reject_result = task_service.reject_task(db_session, task.id, rejecter)

    assert reject_result.ok is False, "Reject phải thất bại vì Run đã chiếm chỗ QUEUED trước"

    verify_session = _second_session_for(db_session)
    try:
        final_task = verify_session.get(DeployTask, task.id)
        assert final_task.status == TaskStatus.QUEUED
        assert final_task.queued_from_status == TaskStatus.TASK
        assert final_task.maintainer_run == "runner@example.com", "Run 'đối thủ' phải đã chiếm chỗ thành công"
    finally:
        verify_session.close()


def test_second_concurrent_run_loses_and_does_not_enqueue_twice(db_session, monkeypatch):
    """2 Run đồng thời trên cùng task: chỉ đúng 1 lần chiếm được QUEUED, lần Run thứ 2
    (thua trong tranh chấp) phải bị chặn. run_task không còn gọi git nên perform_run
    KHÔNG được gọi ở tầng này cho dù ai thắng (đã chuyển sang worker - xem
    test_thread_concurrent_double_run_dequeue_exactly_one_wins bên dưới cho race tương
    đương ở tầng run_queued_task/QUEUED->RUNNING)."""
    runner1 = make_user(db_session, email="runner1@example.com", role=Role.SUPER_ADMIN)
    runner2 = make_user(db_session, email="runner2@example.com", role=Role.SUPER_ADMIN)
    task = make_task(db_session, status=TaskStatus.APPROVED)

    perform_run_calls = []
    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: (perform_run_calls.append(1) or task_service.git_service.RunResult(ok=True, message="ok", image_version="v1")),
    )

    original_cas = task_service._cas_update
    injected = {"done": False}

    def cas_with_injected_other_run_first(db, task_id, condition, values):
        if not injected["done"]:
            injected["done"] = True
            other_session = _second_session_for(db_session)
            try:
                other_run_result = task_service.run_task(other_session, task_id, runner2)
                assert other_run_result.ok is True, "Setup lỗi: Run 'đối thủ' đầu tiên phải thành công"
            finally:
                other_session.close()
        return original_cas(db, task_id, condition, values)

    monkeypatch.setattr(task_service, "_cas_update", cas_with_injected_other_run_first)

    second_run_result = task_service.run_task(db_session, task.id, runner1)

    assert second_run_result.ok is False, "Run thứ 2 phải bị chặn vì task đã Queued"
    assert perform_run_calls == [], "run_task KHÔNG được gọi perform_run - dù ai thắng tranh chấp (đã chuyển sang worker)"

    verify_session = _second_session_for(db_session)
    try:
        final_task = verify_session.get(DeployTask, task.id)
        assert final_task.status == TaskStatus.QUEUED
        assert final_task.maintainer_run == "runner2@example.com", "Chỉ Run đối thủ (thắng) mới được ghi nhận"
    finally:
        verify_session.close()


def test_thread_concurrent_double_run_dequeue_exactly_one_wins(race_session_factory, monkeypatch):
    """Race MỚI được worker (task_service.run_queued_task) thừa hưởng từ run_task cũ:
    2 lần gọi run_queued_task đồng thời trên CÙNG 1 task QUEUED (vd 2 tiến trình worker
    lỡ chạy song song, hoặc job scheduler chồng lượt dù max_instances=1 đã bảo vệ ở tầng
    APScheduler - đây là lớp bảo vệ code-level bổ sung) - CAS (WHERE status=QUEUED) phải
    đảm bảo đúng 1 lần "chiếm chỗ" RUNNING/push, lần thua phải bị chặn và KHÔNG double-push.
    Dùng 2 thread thật + Barrier (như test_thread_concurrent_double_run_exactly_one_wins
    cũ) trên 1 SQLite file-based engine để mô phỏng đúng race thật."""
    setup_session = race_session_factory()
    group = make_group(setup_session)
    make_project(setup_session, group, application="api")
    task = make_task(
        setup_session,
        status=TaskStatus.QUEUED,
        queued_from_status=TaskStatus.APPROVED,
        project=group.group_name,
        application="api",
    )
    task_id = task.id
    setup_session.close()

    perform_run_calls = []
    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: (perform_run_calls.append(1) or task_service.git_service.RunResult(ok=True, message="ok", image_version="v1")),
    )

    barrier = threading.Barrier(2)
    monkeypatch.setattr(task_service, "_cas_update", _barrier_wrap(barrier, task_service._cas_update))

    results: dict[str, object] = {}

    def make_worker(key):
        def worker():
            session = race_session_factory()
            try:
                results[key] = task_service.run_queued_task(session, task_id)
            finally:
                session.close()

        return worker

    t1 = threading.Thread(target=make_worker("w1"))
    t2 = threading.Thread(target=make_worker("w2"))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert not t1.is_alive() and not t2.is_alive(), "Thread bị treo - nghi ngờ deadlock trong CAS/lock"

    outcomes = [results["w1"].ok, results["w2"].ok]
    assert sorted(outcomes) == [False, True], "Đúng 1 trong 2 lần dequeue phải thành công (chiếm RUNNING), lần kia bị chặn"
    assert perform_run_calls == [1], "perform_run phải chỉ được gọi đúng 1 lần dù có 2 worker tranh chấp cùng 1 task"

    verify_session = race_session_factory()
    try:
        final_task = verify_session.get(DeployTask, task_id)
        assert final_task.status == TaskStatus.RUNNING
    finally:
        verify_session.close()


def test_revert_after_git_failure_does_not_clobber_concurrent_done_transition(db_session, monkeypatch):
    """DỜI SANG TẦNG WORKER: trường hợp hy hữu dev có ghi chú trong docstring
    `_revert_or_fail_queued_task` (kế thừa từ `_revert_running_reservation` cũ) - nếu,
    giữa lúc worker (run_queued_task) "chiếm chỗ" RUNNING và lúc revert (do git push lỗi)
    chạy, 1 tiến trình khác (vd scheduler job_check_running_done) đã kịp chuyển task sang
    DONE, thì revert KHÔNG được ghi đè trở lại status cũ - phải giữ nguyên DONE. Trước khi
    có hàng đợi, race này nằm trong run_task(); nay run_task chỉ CAS sang QUEUED (không
    còn gọi git) nên race thật sự nằm ở run_queued_task() (worker)."""
    runner = make_user(db_session, email="runner@example.com", role=Role.SUPER_ADMIN)
    task = make_task(
        db_session, status=TaskStatus.QUEUED, queued_from_status=TaskStatus.APPROVED, maintainer_run=runner.email
    )

    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: task_service.git_service.RunResult(ok=False, message="git push failed: network error"),
    )

    original_cas = task_service._cas_update
    call_count = {"n": 0}

    def cas_intercept_revert(db, task_id, condition, values):
        call_count["n"] += 1
        result = original_cas(db, task_id, condition, values)
        if call_count["n"] == 1:
            # Call dau tien la buoc "chiem cho" RUNNING cua run_queued_task - ngay sau khi
            # no commit, mo phong scheduler khac vua kip chuyen task sang DONE truoc khi
            # revert (call thu 2, trong _revert_or_fail_queued_task) kip chay.
            db.commit()
            other_session = _second_session_for(db_session)
            try:
                other_session.execute(
                    update(DeployTask)
                    .where(DeployTask.id == task_id, DeployTask.status == TaskStatus.RUNNING)
                    .values(status=TaskStatus.DONE, done_at=datetime.utcnow())
                )
                other_session.commit()
            finally:
                other_session.close()
        return result

    monkeypatch.setattr(task_service, "_cas_update", cas_intercept_revert)

    result = task_service.run_queued_task(db_session, task.id)

    assert result.ok is False
    assert "git push failed" in result.message

    verify_session = _second_session_for(db_session)
    try:
        final_task = verify_session.get(DeployTask, task.id)
        assert final_task.status == TaskStatus.DONE, (
            "Revert (CAS WHERE status=RUNNING) không được ghi đè lên trạng thái DONE mà "
            "1 tiến trình khác vừa xác lập - đây chính là lý do dev dùng CAS thay vì gán "
            "thẳng trong _revert_or_fail_queued_task"
        )
    finally:
        verify_session.close()


def test_set_auto_deploy_repeated_identical_call_is_not_a_false_positive_conflict(db_session):
    """Gọi lại set_auto_deploy với ĐÚNG tham số cũ (giá trị thực tế không đổi) không
    được báo xung đột giả - đúng như ghi chú MySYL/CLIENT_FOUND_ROWS trong docstring
    `_cas_update` (ép `updated_at` mới vào values để rowcount luôn phản ánh WHERE có
    khớp hay không, bất kể backend). Test này chạy trên SQLite - xem cảnh báo ở đầu file
    về giới hạn không cover được hành vi rowcount đặc thù của MySQL/pymysql."""
    user = make_user(db_session, role=Role.SUPER_ADMIN)
    task = make_task(db_session, status=TaskStatus.APPROVED)
    run_at = datetime.utcnow() + timedelta(hours=1)

    first = task_service.set_auto_deploy(db_session, task.id, user, True, run_at)
    second = task_service.set_auto_deploy(db_session, task.id, user, True, run_at)

    assert first.ok is True
    assert second.ok is True, "Gọi lại set_auto_deploy với đúng tham số cũ không được báo xung đột giả"


def test_confirm_loses_to_concurrent_cancel(db_session, monkeypatch):
    """Approve và Cancel tranh chấp nhau - Cancel thắng trước thì Approve sau phải bị
    chặn (không thể Approve 1 task vừa bị Cancel)."""
    confirmer = make_user(db_session, email="confirmer@example.com", role=Role.ADMIN)
    canceller = make_user(db_session, email="canceller@example.com", role=Role.USER)
    task = make_task(db_session, status=TaskStatus.TASK, email=canceller.email)

    original_cas = task_service._cas_update
    injected = {"done": False}

    def cas_with_injected_cancel_first(db, task_id, condition, values):
        if not injected["done"]:
            injected["done"] = True
            other_session = _second_session_for(db_session)
            try:
                cancel_result = task_service.cancel_task(other_session, task_id, canceller)
                assert cancel_result.ok is True, "Setup lỗi: Cancel 'đối thủ' phải thành công"
            finally:
                other_session.close()
        return original_cas(db, task_id, condition, values)

    monkeypatch.setattr(task_service, "_cas_update", cas_with_injected_cancel_first)

    confirm_result = task_service.approve_task(db_session, task.id, confirmer)

    assert confirm_result.ok is False, "Approve phải thất bại vì Cancel đã thắng trước"

    verify_session = _second_session_for(db_session)
    try:
        final_task = verify_session.get(DeployTask, task.id)
        assert final_task.status == TaskStatus.CANCELLED
    finally:
        verify_session.close()


def test_cancel_queued_task_loses_to_concurrent_worker_dequeue(db_session, monkeypatch):
    """RACE MỚI phát sinh từ hàng đợi (chưa từng tồn tại trước đây): user bấm Cancel 1
    task đang QUEUED đúng lúc worker (run_queued_task) vừa kịp CAS QUEUED -> RUNNING và
    bắt đầu push git - Cancel PHẢI thất bại (không được để 1 task ĐÃ THỰC SỰ CHẠY GIT bị
    đánh dấu Cancelled), và ngược lại task phải chạy tới cùng bình thường."""
    admin = make_user(db_session, email="cancel-race-admin@example.com", role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.QUEUED, queued_from_status=TaskStatus.APPROVED)

    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: task_service.git_service.RunResult(ok=True, message="ok", image_version="v1"),
    )

    original_cas = task_service._cas_update
    injected = {"done": False}

    def cas_with_injected_worker_dequeue_first(db, task_id, condition, values):
        if not injected["done"]:
            injected["done"] = True
            other_session = _second_session_for(db_session)
            try:
                worker_result = task_service.run_queued_task(other_session, task_id)
                assert worker_result.ok is True, "Setup lỗi: worker 'đối thủ' phải dequeue+chạy thành công"
            finally:
                other_session.close()
        return original_cas(db, task_id, condition, values)

    monkeypatch.setattr(task_service, "_cas_update", cas_with_injected_worker_dequeue_first)

    cancel_result = task_service.cancel_task(db_session, task.id, admin)

    assert cancel_result.ok is False, "Cancel phải thất bại vì worker đã kịp dequeue task sang RUNNING trước"

    verify_session = _second_session_for(db_session)
    try:
        final_task = verify_session.get(DeployTask, task.id)
        assert final_task.status == TaskStatus.RUNNING, "Task đã được worker chạy thật, không được biến thành Cancelled"
        assert final_task.image_version == "v1"
    finally:
        verify_session.close()


def test_worker_dequeue_loses_to_concurrent_cancel(db_session, monkeypatch):
    """Chiều ngược lại của race trên: nếu Cancel thắng trước (chuyển QUEUED -> CANCELLED),
    worker đến sau PHẢI bị chặn (không được push git cho 1 task đã bị huỷ) - đây chính là
    điều kiện Leader yêu cầu xác nhận: 'Task đã Cancel vẫn bị worker chạy' KHÔNG được xảy ra."""
    canceller = make_user(db_session, email="cancel-race-user@example.com", role=Role.USER)
    task = make_task(
        db_session, status=TaskStatus.QUEUED, queued_from_status=TaskStatus.APPROVED, email=canceller.email
    )

    perform_run_calls = []
    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: (perform_run_calls.append(1) or task_service.git_service.RunResult(ok=True, message="ok", image_version="v1")),
    )

    original_cas = task_service._cas_update
    injected = {"done": False}

    def cas_with_injected_cancel_first(db, task_id, condition, values):
        if not injected["done"]:
            injected["done"] = True
            other_session = _second_session_for(db_session)
            try:
                cancel_result = task_service.cancel_task(other_session, task_id, canceller)
                assert cancel_result.ok is True, "Setup lỗi: Cancel 'đối thủ' phải thành công"
            finally:
                other_session.close()
        return original_cas(db, task_id, condition, values)

    monkeypatch.setattr(task_service, "_cas_update", cas_with_injected_cancel_first)

    worker_result = task_service.run_queued_task(db_session, task.id)

    assert worker_result.ok is False, "Worker phải bị chặn vì Cancel đã thắng trước"
    assert perform_run_calls == [], "NGUY HIỂM: worker vẫn push git cho task đã bị Cancel"

    verify_session = _second_session_for(db_session)
    try:
        final_task = verify_session.get(DeployTask, task.id)
        assert final_task.status == TaskStatus.CANCELLED
    finally:
        verify_session.close()


# ---------------------------------------------------------------------------
# Nhóm 2: thread thật + file-based SQLite engine (mỗi thread 1 connection riêng)
# ---------------------------------------------------------------------------


@pytest.fixture()
def race_session_factory():
    fd, path = tempfile.mkstemp(suffix="-race.db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 30})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    yield factory
    engine.dispose()
    try:
        os.unlink(path)
    except OSError:
        pass


def _barrier_wrap(barrier, original_fn):
    def wrapped(db, task_id, condition, values):
        try:
            barrier.wait(timeout=10)
        except threading.BrokenBarrierError:
            pass
        return original_fn(db, task_id, condition, values)

    return wrapped


def test_thread_concurrent_run_and_reject_exactly_one_wins(race_session_factory, monkeypatch):
    """Dùng 2 thread thật, mỗi thread 1 Session/connection SQLite riêng (khác connection
    dùng chung của fixture `db_session` mặc định - không phù hợp để mô phỏng ghi đồng
    thời thật sự), đồng bộ bằng Barrier ngay trước câu UPDATE thật để ép 2 thread cùng
    tranh chấp ghi vào đúng 1 thời điểm."""
    setup_session = race_session_factory()
    group = make_group(setup_session)
    make_project(setup_session, group, application="api")
    task = make_task(setup_session, status=TaskStatus.APPROVED, project=group.group_name, application="api")
    make_user(setup_session, email="runner@example.com", role=Role.SUPER_ADMIN)
    make_user(setup_session, email="rejecter@example.com", role=Role.ADMIN)
    task_id = task.id
    setup_session.close()

    perform_run_calls = []
    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: (perform_run_calls.append(1) or task_service.git_service.RunResult(ok=True, message="ok", image_version="v1")),
    )

    barrier = threading.Barrier(2)
    monkeypatch.setattr(task_service, "_cas_update", _barrier_wrap(barrier, task_service._cas_update))

    results: dict[str, object] = {}

    def run_worker():
        # QUAN TRỌNG: mỗi thread phải tự query lại User bằng Session CỦA RIÊNG NÓ - dùng
        # lại instance ORM đã tạo qua `setup_session` (đã close/expired) từ thread khác sẽ
        # ném DetachedInstanceError không liên quan gì đến race condition đang test.
        session = race_session_factory()
        try:
            runner = session.query(User).filter(User.email == "runner@example.com").one()
            results["run"] = task_service.run_task(session, task_id, runner)
        finally:
            session.close()

    def reject_worker():
        session = race_session_factory()
        try:
            rejecter = session.query(User).filter(User.email == "rejecter@example.com").one()
            results["reject"] = task_service.reject_task(session, task_id, rejecter)
        finally:
            session.close()

    t1 = threading.Thread(target=run_worker)
    t2 = threading.Thread(target=reject_worker)
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert not t1.is_alive() and not t2.is_alive(), "Thread bị treo - nghi ngờ deadlock trong CAS/lock"

    run_result = results["run"]
    reject_result = results["reject"]

    assert run_result.ok != reject_result.ok, "Đúng 1 trong 2 thao tác (Run/Reject) phải thành công, không thể cả hai"

    verify_session = race_session_factory()
    try:
        final_task = verify_session.get(DeployTask, task_id)
        if run_result.ok:
            # Run KHÔNG còn chiếm RUNNING/push git ngay trên đường request - chỉ chiếm
            # QUEUED (xem TaskStatus.QUEUED/task_service.run_task).
            assert final_task.status == TaskStatus.QUEUED
            assert perform_run_calls == [], "run_task KHÔNG được gọi perform_run (đã chuyển sang worker)"
            assert reject_result.ok is False
        else:
            # Reject thắng - kịch bản nguy hiểm nhất: Run KHÔNG được push
            assert final_task.status == TaskStatus.REJECTED
            assert perform_run_calls == [], "NGUY HIỂM: Run vẫn push code dù Reject đã thắng (thread thật)"
            assert reject_result.ok is True
    finally:
        verify_session.close()


def test_thread_concurrent_double_run_exactly_one_wins(race_session_factory, monkeypatch):
    """2 Run đồng thời (thread thật) trên CÙNG 1 task: CAS (TASK/APPROVED -> QUEUED) phải
    đảm bảo đúng 1 lần chiếm được QUEUED, lần kia bị chặn - perform_run KHÔNG được gọi ở
    tầng run_task nữa (đã chuyển sang worker, xem
    test_thread_concurrent_double_run_dequeue_exactly_one_wins cho race ở tầng
    run_queued_task/QUEUED->RUNNING)."""
    setup_session = race_session_factory()
    group = make_group(setup_session)
    make_project(setup_session, group, application="api")
    task = make_task(setup_session, status=TaskStatus.APPROVED, project=group.group_name, application="api")
    make_user(setup_session, email="runner1@example.com", role=Role.SUPER_ADMIN)
    make_user(setup_session, email="runner2@example.com", role=Role.SUPER_ADMIN)
    task_id = task.id
    setup_session.close()

    perform_run_calls = []
    monkeypatch.setattr(
        task_service.git_service,
        "perform_run",
        lambda *a, **k: (perform_run_calls.append(1) or task_service.git_service.RunResult(ok=True, message="ok", image_version="v1")),
    )

    barrier = threading.Barrier(2)
    monkeypatch.setattr(task_service, "_cas_update", _barrier_wrap(barrier, task_service._cas_update))

    results: dict[str, object] = {}

    def make_worker(key, email):
        # QUAN TRỌNG: query lại User bằng Session riêng của từng thread (xem ghi chú ở
        # test_thread_concurrent_run_and_reject_exactly_one_wins).
        def worker():
            session = race_session_factory()
            try:
                user = session.query(User).filter(User.email == email).one()
                results[key] = task_service.run_task(session, task_id, user)
            finally:
                session.close()

        return worker

    t1 = threading.Thread(target=make_worker("r1", "runner1@example.com"))
    t2 = threading.Thread(target=make_worker("r2", "runner2@example.com"))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert not t1.is_alive() and not t2.is_alive(), "Thread bị treo - nghi ngờ deadlock trong CAS/lock"

    outcomes = [results["r1"].ok, results["r2"].ok]
    assert sorted(outcomes) == [False, True], "Đúng 1 trong 2 lần Run phải thành công (chiếm QUEUED), lần kia bị chặn"
    assert perform_run_calls == [], "run_task KHÔNG được gọi perform_run (đã chuyển sang worker)"

    verify_session = race_session_factory()
    try:
        final_task = verify_session.get(DeployTask, task_id)
        assert final_task.status == TaskStatus.QUEUED
    finally:
        verify_session.close()


# ---------------------------------------------------------------------------
# Nhóm 3: file lock git (`app/services/git_service._repo_lock`)
# ---------------------------------------------------------------------------


def test_repo_lock_serializes_concurrent_critical_sections(tmp_path):
    """2 thread cùng xin `_repo_lock` trên CÙNG 1 checkout directory phải bị tuần tự
    hoá tuyệt đối - khoảng thời gian [enter, exit] của 2 thread không được chồng lấn."""
    directory = str(tmp_path / "checkout-master")
    events: list[tuple[str, str, float]] = []
    events_guard = threading.Lock()

    def worker(label: str) -> None:
        with git_service._repo_lock(directory, label):
            with events_guard:
                events.append((label, "enter", time.monotonic()))
            time.sleep(0.15)
            with events_guard:
                events.append((label, "exit", time.monotonic()))

    t1 = threading.Thread(target=worker, args=("A",))
    t2 = threading.Thread(target=worker, args=("B",))
    t1.start()
    time.sleep(0.02)
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not t1.is_alive() and not t2.is_alive()
    assert len(events) == 4

    intervals: dict[str, dict[str, float]] = {}
    for label, kind, ts in events:
        intervals.setdefault(label, {})[kind] = ts

    (a_enter, a_exit), (b_enter, b_exit) = (
        (intervals["A"]["enter"], intervals["A"]["exit"]),
        (intervals["B"]["enter"], intervals["B"]["exit"]),
    )
    overlap = a_enter < b_exit and b_enter < a_exit
    assert not overlap, f"2 critical section chồng lấn nhau dù đã bọc bằng _repo_lock: {events}"


def test_repo_lock_raises_git_error_on_timeout_when_lock_held_elsewhere(tmp_path, monkeypatch):
    """Nếu không lấy được lock trong `_GIT_LOCK_TIMEOUT_SECONDS`, `_repo_lock` phải raise
    `GitError` (không được treo vô hạn hoặc raise lỗi loại khác khó xử lý ở tầng gọi)."""
    directory = str(tmp_path / "checkout-master")
    monkeypatch.setattr(git_service, "_GIT_LOCK_TIMEOUT_SECONDS", 0.3)

    lock_path = git_service._lock_file_path(directory)
    holder = FileLock(str(lock_path))
    holder.acquire()
    try:
        with pytest.raises(git_service.GitError, match="đang bận"):
            with git_service._repo_lock(directory, "master"):
                pytest.fail("Không được vào critical section khi chưa lấy được lock")
    finally:
        holder.release()


def test_repo_lock_staging_and_master_are_independent_locks(tmp_path):
    """staging_repo_lock và master_repo_lock phải là 2 lock ĐỘC LẬP (khác file) - 1 thread
    giữ lock staging không được chặn 1 thread khác xin lock master (và ngược lại), nếu
    không sẽ vô tình làm chậm/deadlock giữa luồng sync catalog (staging) và luồng
    Run/Rollback (master) dù chúng thao tác 2 checkout khác nhau."""
    staging_dir = str(tmp_path / "repo-staging")
    master_dir = str(tmp_path / "repo-master")

    entered_master = threading.Event()

    def hold_staging():
        with git_service._repo_lock(staging_dir, "staging"):
            entered_master.wait(timeout=5)
            time.sleep(0.05)

    t_staging = threading.Thread(target=hold_staging)
    t_staging.start()
    time.sleep(0.05)

    started = time.monotonic()
    with git_service._repo_lock(master_dir, "master"):
        entered_master.set()
        elapsed = time.monotonic() - started

    t_staging.join(timeout=5)
    assert not t_staging.is_alive()
    assert elapsed < 1.0, "Lock master bị chặn bởi lock staging đang được giữ - 2 lock không độc lập"
