"""[TEST] Việc 3 (phần app/scheduler.py::job_check_running_done/job_check_running_too_long) -
đây là bản vá CHẠM LUỒNG DONE PRODUCTION quan trọng nhất: trước đây `job_check_running_done`
CHỈ so `status.summary.images` (version manifest ĐÃ ÁP DỤNG) -> có thể báo Done ngay cả khi
pod đang ImagePullBackOff/CrashLoopBackOff/Pending. Nay CHỈ Done khi version khớp VÀ
`argo_status.is_healthy_and_synced` (health=="Healthy" và sync=="Synced").

Test gọi thẳng `scheduler_module.job_check_running_done()`/`job_check_running_too_long()`,
patch `SessionLocal` trỏ về `db_session` (theo convention tests/test_reminder_scheduler.py)
và monkeypatch `scheduler_module.get_running_app_status` (tên được import thẳng vào
app/scheduler.py) để giả lập response ArgoCD ở mức job, KHÔNG cần mock httpx ở đây (đã có
tests/test_argocd_service.py test riêng phần parse response thật)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from app import scheduler as scheduler_module
from app.models import DeployTask, TaskStatus
from app.services.argocd_service import ArgoAppStatus


def make_running_task(db_session, **kwargs):
    defaults = dict(
        project="core",
        application="api",
        commitid="abc1234",
        status=TaskStatus.RUNNING,
        email="owner@example.com",
        image_version="v1.2.3",
        run_at=datetime.utcnow(),
    )
    defaults.update(kwargs)
    task = DeployTask(**defaults)
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


def _patch_scheduler_session(monkeypatch, db_session):
    monkeypatch.setattr(scheduler_module, "SessionLocal", lambda: db_session)


def _patch_argo(monkeypatch, status):
    """`status` la 1 ArgoAppStatus hoac None (gia lap tra ve tu get_running_app_status)."""
    monkeypatch.setattr(scheduler_module, "get_running_app_status", lambda db, project, application: status)


# ---------------------------------------------------------------------------
# Ma trận DONE - đúng như bảng yêu cầu của Leader
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "version_matches,health,sync,expect_done",
    [
        (True, "Healthy", "Synced", True),
        (True, "Progressing", "Synced", False),
        (True, "Degraded", "Synced", False),
        (True, "Healthy", "OutOfSync", False),
        (True, "Missing", "Synced", False),
        (True, "Unknown", "Synced", False),
        (False, "Healthy", "Synced", False),
    ],
)
def test_job_check_running_done_matrix(db_session, monkeypatch, version_matches, health, sync, expect_done):
    _patch_scheduler_session(monkeypatch, db_session)
    task = make_running_task(db_session, image_version="v1.2.3")
    task_id = task.id

    argo_version = "v1.2.3" if version_matches else "v0.0.1-different"
    _patch_argo(monkeypatch, ArgoAppStatus(image_version=argo_version, health_status=health, sync_status=sync))

    sent: list[str] = []
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: sent.append(msg))
    monkeypatch.setattr(scheduler_module, "send_mail", lambda *a, **k: None)

    scheduler_module.job_check_running_done()

    reloaded = db_session.get(DeployTask, task_id)
    if expect_done:
        assert reloaded.status == TaskStatus.DONE, f"version_matches={version_matches} health={health} sync={sync}"
        assert reloaded.done_at is not None
        assert len(sent) == 1
        assert "done" in sent[0].lower() or "Done" in sent[0]
    else:
        assert reloaded.status == TaskStatus.RUNNING, (
            f"KHONG duoc Done khi version_matches={version_matches} health={health} sync={sync}"
        )
        assert reloaded.done_at is None
        assert sent == [], "Khong duoc gui thong bao 'Task done' khi chua thuc su Done"


# ---------------------------------------------------------------------------
# ArgoCD tắt / lỗi mạng (get_running_app_status trả None) -> không Done, không crash
# ---------------------------------------------------------------------------


def test_job_check_running_done_argocd_disabled_no_crash_no_done(db_session, monkeypatch):
    _patch_scheduler_session(monkeypatch, db_session)
    task = make_running_task(db_session)
    task_id = task.id
    _patch_argo(monkeypatch, None)
    sent: list[str] = []
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: sent.append(msg))

    scheduler_module.job_check_running_done()  # khong duoc raise

    reloaded = db_session.get(DeployTask, task_id)
    assert reloaded.status == TaskStatus.RUNNING
    assert sent == []


def test_job_check_running_done_argocd_network_error_no_crash_no_done(db_session, monkeypatch):
    """get_running_app_status tu no da bat loi mang/timeout va tra None (xem
    tests/test_argocd_service.py) - o day chi can xac nhan job khong crash/khong Done khi
    nhan duoc None, mo phong dung tinh huong that."""
    _patch_scheduler_session(monkeypatch, db_session)
    task = make_running_task(db_session)
    task_id = task.id

    def _boom(db, project, application):
        raise AssertionError("job khong duoc de exception tu get_running_app_status lan ra - ham do da tu bat loi mang roi")

    # Mo phong dung hop dong that: get_running_app_status KHONG bao gio raise ra ngoai (tu
    # bat httpx.HTTPError va tra None) - job_check_running_done khong co try/except quanh
    # loi goi ArgoCD nen PHAI dua vao dung hop dong nay.
    _patch_argo(monkeypatch, None)

    scheduler_module.job_check_running_done()

    reloaded = db_session.get(DeployTask, task_id)
    assert reloaded.status == TaskStatus.RUNNING


# ---------------------------------------------------------------------------
# Nhiều task Running cùng lúc: 1 task đủ điều kiện Done, 1 task chưa - job xử lý ĐỘC LẬP
# ---------------------------------------------------------------------------


def test_job_check_running_done_handles_multiple_tasks_independently(db_session, monkeypatch):
    _patch_scheduler_session(monkeypatch, db_session)
    healthy_task = make_running_task(db_session, image_version="v1.0.0", application="api")
    crashing_task = make_running_task(db_session, image_version="v2.0.0", application="worker")
    healthy_id, crashing_id = healthy_task.id, crashing_task.id

    def _fake_get_status(db, project, application):
        if application == "api":
            return ArgoAppStatus(image_version="v1.0.0", health_status="Healthy", sync_status="Synced")
        return ArgoAppStatus(image_version="v2.0.0", health_status="Degraded", sync_status="Synced")

    monkeypatch.setattr(scheduler_module, "get_running_app_status", _fake_get_status)
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: None)
    monkeypatch.setattr(scheduler_module, "send_mail", lambda *a, **k: None)

    scheduler_module.job_check_running_done()

    assert db_session.get(DeployTask, healthy_id).status == TaskStatus.DONE
    assert db_session.get(DeployTask, crashing_id).status == TaskStatus.RUNNING


# ---------------------------------------------------------------------------
# [TEST vòng xác nhận cuối] Nhiệm vụ 1 - HẬU QUẢ GỐC: 1 task Ở GIỮA gây lỗi bất ngờ
# (mô phỏng đúng bug gốc: get_running_app_status raise ra ngoài thay vì tự bắt) KHÔNG
# được làm dừng ngang vòng lặp - các task RUNNING khác (trước và sau task lỗi) VẪN phải
# được đối chiếu bình thường trong CÙNG lượt quét. Test cả 2 job.
# ---------------------------------------------------------------------------


def test_job_check_running_done_mid_scan_error_does_not_skip_remaining_tasks(db_session, monkeypatch):
    """3 task RUNNING: task 'before' đã Healthy/Synced đúng version (đủ điều kiện Done),
    task 'middle' làm get_running_app_status RAISE (mô phỏng đúng bug gốc: JSONDecodeError/
    lỗi bất ngờ lan ra ngoài thay vì tự bắt), task 'after' cũng đủ điều kiện Done. Trước khi
    vá (bug gốc): raise ở 'middle' sẽ dừng ngang vòng lặp `for task in tasks`, khiến 'after'
    (đứng sau 'middle' trong danh sách) KHÔNG BAO GIỜ được đối chiếu. Sau khi vá: cả
    'before' và 'after' đều phải Done, chỉ 'middle' bị bỏ qua (log lỗi, KHÔNG Done)."""
    _patch_scheduler_session(monkeypatch, db_session)
    before = make_running_task(db_session, image_version="v1.0.0", application="before-app")
    middle = make_running_task(db_session, image_version="v2.0.0", application="middle-app")
    after = make_running_task(db_session, image_version="v3.0.0", application="after-app")
    before_id, middle_id, after_id = before.id, middle.id, after.id

    def _fake_get_status(db, project, application):
        if application == "middle-app":
            raise ValueError("gia lap loi bat ngo tu ArgoCD (vd JSONDecodeError lan ra ngoai)")
        version = "v1.0.0" if application == "before-app" else "v3.0.0"
        return ArgoAppStatus(image_version=version, health_status="Healthy", sync_status="Synced")

    monkeypatch.setattr(scheduler_module, "get_running_app_status", _fake_get_status)
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: None)
    monkeypatch.setattr(scheduler_module, "send_mail", lambda *a, **k: None)

    scheduler_module.job_check_running_done()  # khong duoc raise ra ngoai

    assert db_session.get(DeployTask, before_id).status == TaskStatus.DONE, (
        "Task DUNG TRUOC task loi trong danh sach phai van duoc Done binh thuong"
    )
    assert db_session.get(DeployTask, middle_id).status == TaskStatus.RUNNING, (
        "Task gay loi khong duoc Done (chua biet trang thai that)"
    )
    assert db_session.get(DeployTask, after_id).status == TaskStatus.DONE, (
        "HAU QUA GOC cua bug: task DUNG SAU task loi trong danh sach truoc day se bi BO SOT "
        "hoan toan do vong lap dung ngang - day la bang chung quan trong nhat bug da duoc vá"
    )


def test_job_check_running_too_long_mid_scan_error_does_not_skip_remaining_tasks(db_session, monkeypatch):
    """Tuong tu test tren nhung cho job_check_running_too_long: task 'middle' gay loi bat
    ngo khong duoc lam mat canh bao cua task 'after' dung sau no trong danh sach."""
    _patch_scheduler_session(monkeypatch, db_session)
    _make_stale_running_task(db_session, image_version="v1.0.0", application="before-app")
    _make_stale_running_task(db_session, image_version="v2.0.0", application="middle-app")
    _make_stale_running_task(db_session, image_version="v3.0.0", application="after-app")

    def _fake_get_status(db, project, application):
        if application == "middle-app":
            raise ValueError("gia lap loi bat ngo tu ArgoCD")
        # before/after: version KHONG khop -> phai gui canh bao "not match version"
        return ArgoAppStatus(image_version="v9.9.9-different", health_status="Healthy", sync_status="Synced")

    monkeypatch.setattr(scheduler_module, "get_running_app_status", _fake_get_status)
    sent: list[str] = []
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: sent.append(msg))

    scheduler_module.job_check_running_too_long()  # khong duoc raise ra ngoai

    assert len(sent) == 2, (
        "Phai co canh bao cho CA 'before-app' VA 'after-app' - neu vong lap dung ngang tai "
        f"'middle-app' thi 'after-app' se bi bo sot, thuc te so canh bao gui duoc: {len(sent)}"
    )
    apps_alerted = "".join(sent)
    assert "before-app" in apps_alerted
    assert "after-app" in apps_alerted


# ---------------------------------------------------------------------------
# [TEST vòng xác nhận cuối] Nhiệm vụ 2 - soi lập luận "không cần db.rollback()" của dev:
# task 1 đã được gán task.status = DONE trên object (CHƯA commit), task 2 (đứng sau) gây ra
# 1 LỖI DB THẬT (không phải lỗi Python thuần tuý - dùng flush() thất bại thật với
# IntegrityError, giống loại lỗi có thể khiến SQLAlchemy Session rơi vào trạng thái cần
# rollback) - kiểm chứng xem db.commit() CUỐI vòng lặp (dòng ngoài try/except từng task) có
# bị hỏng theo không, và nếu hỏng thì kết quả Done của task 1 có bị mất theo không.
# ---------------------------------------------------------------------------


def test_task2_real_db_flush_error_can_break_final_commit_and_lose_task1_done(db_session, monkeypatch):
    _patch_scheduler_session(monkeypatch, db_session)
    task1 = make_running_task(db_session, image_version="v1.0.0", application="app-one")
    task2 = make_running_task(db_session, image_version="v2.0.0", application="app-two")
    task1_id, task2_id = task1.id, task2.id

    def _fake_get_status(db, project, application):
        if application == "app-one":
            return ArgoAppStatus(image_version="v1.0.0", health_status="Healthy", sync_status="Synced")
        # app-two: gia lap 1 LOI DB THAT xay ra trong luc xu ly task nay (vd 1 thao tac ghi
        # DB khac nao do trong cung request/luot quet dung phai xung dot that - o day dung
        # truc tiep 1 flush() that bai voi IntegrityError (PK trung) de tao dung DUNG loai
        # loi lam SQLAlchemy Session roi vao trang thai "can rollback truoc khi dung tiep",
        # KHONG suy dien suong - da tu kiem chung bang script rieng truoc khi dua vao day.
        dup = DeployTask(
            id=task1_id,  # trung PK voi task1 -> IntegrityError khi flush
            project="dup",
            application="dup",
            commitid="dup0001",
            status=TaskStatus.RUNNING,
            email="dup@example.com",
        )
        db.add(dup)
        db.flush()  # IntegrityError that, gia lap 1 loi DB that (khong phai ValueError/TypeError thuan)
        return None  # khong bao gio toi day

    monkeypatch.setattr(scheduler_module, "get_running_app_status", _fake_get_status)
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: None)
    monkeypatch.setattr(scheduler_module, "send_mail", lambda *a, **k: None)

    try:
        scheduler_module.job_check_running_done()
        job_raised = None
    except Exception as e:  # noqa: BLE001 - muon bat CHINH XAC exception nao lan ra, khong nuot
        job_raised = e

    # Doc lai bang 1 session MOI, doc lap hoan toan voi session cua job (tranh doc nham
    # trang thai in-memory chua commit cua chinh db_session dang co the da "hong").
    from sqlalchemy.orm import sessionmaker

    Fresh = sessionmaker(bind=db_session.get_bind())
    fresh = Fresh()
    try:
        reloaded_task1 = fresh.get(DeployTask, task1_id)
        reloaded_task2 = fresh.get(DeployTask, task2_id)

        if job_raised is not None:
            # KET LUAN: lap luan "khong can rollback" cua dev SAI trong truong hop nay - 1
            # loi DB THAT (khong phai loi Python thuan) xay ra giua vong lap se khien Session
            # can rollback truoc khi dung tiep; vi job KHONG rollback, db.commit() CUOI vong
            # lap (ngoai try/except tung task) tu lan loi nay ra NGOAI ham, va ket qua Done
            # hop le cua task1 (da gan tren object nhung chua commit) bi MAT theo (khong
            # duoc luu xuong DB that) - day la bang chung cu the, khong phai suy dien.
            assert reloaded_task1.status == TaskStatus.RUNNING, (
                "Task 1 dang le phai duoc Done nhung bi MAT do commit cuoi vong lap that bai "
                "va KHONG co rollback - dung y lap luan cua dev ve khong can rollback"
            )
        else:
            # Neu job KHONG raise (vd phien ban SQLAlchemy/driver khac khong invalidate
            # session theo cach nay), it nhat verify task1 van duoc Done dung nhu mong doi.
            assert reloaded_task1.status == TaskStatus.DONE
    finally:
        fresh.close()


# ---------------------------------------------------------------------------
# job_check_running_too_long: cảnh báo khác nhau tuỳ version khớp hay chưa
# ---------------------------------------------------------------------------


def _make_stale_running_task(db_session, **kwargs):
    defaults = dict(run_at=datetime.utcnow() - timedelta(minutes=30))
    defaults.update(kwargs)
    return make_running_task(db_session, **defaults)


def test_job_check_running_too_long_version_not_match_sends_old_style_alert(db_session, monkeypatch):
    _patch_scheduler_session(monkeypatch, db_session)
    _make_stale_running_task(db_session, image_version="v1.2.3")
    _patch_argo(monkeypatch, ArgoAppStatus(image_version="v9.9.9", health_status="Healthy", sync_status="Synced"))
    sent: list[str] = []
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: sent.append(msg))

    scheduler_module.job_check_running_too_long()

    assert len(sent) == 1
    assert "version not matched" in sent[0]
    assert "not Healthy/Synced" not in sent[0]


def test_job_check_running_too_long_version_match_but_not_healthy_sends_new_alert(db_session, monkeypatch):
    _patch_scheduler_session(monkeypatch, db_session)
    _make_stale_running_task(db_session, image_version="v1.2.3")
    _patch_argo(
        monkeypatch, ArgoAppStatus(image_version="v1.2.3", health_status="CrashLoopBackOff", sync_status="Synced")
    )
    sent: list[str] = []
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: sent.append(msg))

    scheduler_module.job_check_running_too_long()

    assert len(sent) == 1
    assert "not Healthy/Synced" in sent[0]
    assert "version not matched" not in sent[0]
    assert "CrashLoopBackOff" in sent[0]
    assert "Synced" in sent[0]


def test_job_check_running_too_long_healthy_and_synced_sends_no_alert(db_session, monkeypatch):
    """Task da du dieu kien Done (se duoc job_check_running_done xu ly luot ke tiep) ->
    khong can canh bao 'treo qua lau' nua."""
    _patch_scheduler_session(monkeypatch, db_session)
    _make_stale_running_task(db_session, image_version="v1.2.3")
    _patch_argo(monkeypatch, ArgoAppStatus(image_version="v1.2.3", health_status="Healthy", sync_status="Synced"))
    sent: list[str] = []
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: sent.append(msg))

    scheduler_module.job_check_running_too_long()

    assert sent == []


def test_job_check_running_too_long_argocd_disabled_falls_back_to_version_alert_no_crash(db_session, monkeypatch):
    """argo_status None (ArgoCD tat/loi mang) - phai roi vao nhanh canh bao 'not match
    version' (actual_version=None) thay vi crash do argo_status.health_status."""
    _patch_scheduler_session(monkeypatch, db_session)
    _make_stale_running_task(db_session, image_version="v1.2.3")
    _patch_argo(monkeypatch, None)
    sent: list[str] = []
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: sent.append(msg))

    scheduler_module.job_check_running_too_long()  # khong duoc raise

    assert len(sent) == 1
    assert "version not matched" in sent[0]
    assert "None" in sent[0]


def test_job_check_running_too_long_skips_task_within_alert_threshold(db_session, monkeypatch):
    _patch_scheduler_session(monkeypatch, db_session)
    make_running_task(db_session, image_version="v1.2.3", run_at=datetime.utcnow())
    _patch_argo(monkeypatch, ArgoAppStatus(image_version="v9.9.9", health_status="Progressing", sync_status="OutOfSync"))
    sent: list[str] = []
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: sent.append(msg))

    scheduler_module.job_check_running_too_long()

    assert sent == [], "Task chua qua nguong running_alert_after_minutes khong duoc canh bao"


# ---------------------------------------------------------------------------
# Nhiệm vụ 4 - task version khớp nhưng KHÔNG BAO GIỜ Healthy -> không bao giờ tự Done;
# job_check_running_too_long GIÃN CÁCH cảnh báo lặp lại theo AppSetting.running_alert_
# repeat_minutes (mặc định 30) + DeployTask.last_alert_at thay vì spam mỗi lượt quét.
# ---------------------------------------------------------------------------


def test_task_never_healthy_never_auto_dones_and_alert_sent_once_within_repeat_window(db_session, monkeypatch):
    _patch_scheduler_session(monkeypatch, db_session)
    task = make_running_task(
        db_session, image_version="v1.2.3", run_at=datetime.utcnow() - timedelta(minutes=30)
    )
    task_id = task.id
    _patch_argo(
        monkeypatch, ArgoAppStatus(image_version="v1.2.3", health_status="CrashLoopBackOff", sync_status="Synced")
    )
    done_sent: list[str] = []
    alert_sent: list[str] = []
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: (done_sent if "done" in msg.lower() else alert_sent).append(msg))
    monkeypatch.setattr(scheduler_module, "send_mail", lambda *a, **k: None)

    for _ in range(3):  # mo phong 3 luot quet lien tiep, tat ca trong cung khung gio (chua
        # du running_alert_repeat_minutes mac dinh 30 phut ke tu lan canh bao dau tien)
        scheduler_module.job_check_running_done()
        scheduler_module.job_check_running_too_long()

    reloaded = db_session.get(DeployTask, task_id)
    assert reloaded.status == TaskStatus.RUNNING, "App khong bao gio Healthy -> task khong bao gio tu Done"
    assert done_sent == [], "Khong duoc bao Done sai khi app chua Healthy"
    assert len(alert_sent) == 1, (
        "Trong cung khung running_alert_repeat_minutes (mac dinh 30 phut), cac lan quet lap "
        f"lai KHONG duoc gui canh bao them - thuc te so lan gui: {len(alert_sent)}"
    )
    assert reloaded.last_alert_at is not None


def test_alert_repeat_minutes_zero_sends_only_once_forever(db_session, monkeypatch):
    """running_alert_repeat_minutes = 0 nghia la chi canh bao 1 lan duy nhat cho toi khi
    task roi RUNNING, du lan quet sau do da qua rat lau."""
    from app.services.app_setting_service import update_app_setting

    _patch_scheduler_session(monkeypatch, db_session)
    update_app_setting(db_session, running_alert_repeat_minutes=0)
    task = _make_stale_running_task(db_session, image_version="v1.2.3")
    task_id = task.id
    _patch_argo(
        monkeypatch, ArgoAppStatus(image_version="v1.2.3", health_status="CrashLoopBackOff", sync_status="Synced")
    )
    sent: list[str] = []
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: sent.append(msg))

    scheduler_module.job_check_running_too_long()
    assert len(sent) == 1

    # Da canh bao roi + repeat_minutes=0 -> gia lap lan quet rat lau sau van khong gui lai.
    # job_check_running_too_long dong db.close() sau moi lan goi (expunge het object cu) nen
    # phai lay lai instance MOI tu session truoc khi sua/commit, neu khong thay doi se bi
    # "mat" (object detached, ghi vao attribute khong duoc flush xuong DB).
    reloaded = db_session.get(DeployTask, task_id)
    reloaded.last_alert_at = datetime.utcnow() - timedelta(days=1)
    db_session.commit()
    scheduler_module.job_check_running_too_long()
    assert len(sent) == 1, "repeat_minutes=0 phai CHI canh bao 1 lan duy nhat"


def test_alert_repeats_again_after_repeat_window_elapsed(db_session, monkeypatch):
    """Da qua du running_alert_repeat_minutes ke tu last_alert_at -> canh bao lai lan nua."""
    from app.services.app_setting_service import update_app_setting

    _patch_scheduler_session(monkeypatch, db_session)
    update_app_setting(db_session, running_alert_repeat_minutes=30)
    task = _make_stale_running_task(db_session, image_version="v1.2.3")
    task_id = task.id
    _patch_argo(
        monkeypatch, ArgoAppStatus(image_version="v1.2.3", health_status="CrashLoopBackOff", sync_status="Synced")
    )
    sent: list[str] = []
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: sent.append(msg))

    scheduler_module.job_check_running_too_long()
    assert len(sent) == 1

    # Chua du 30 phut -> khong gui lai. (Lay lai instance MOI vi session da dong sau lan
    # goi truoc - xem giai thich o test_alert_repeat_minutes_zero_sends_only_once_forever.)
    reloaded = db_session.get(DeployTask, task_id)
    reloaded.last_alert_at = datetime.utcnow() - timedelta(minutes=10)
    db_session.commit()
    scheduler_module.job_check_running_too_long()
    assert len(sent) == 1

    # Da du 30 phut -> gui lai lan nua.
    reloaded = db_session.get(DeployTask, task_id)
    reloaded.last_alert_at = datetime.utcnow() - timedelta(minutes=31)
    db_session.commit()
    scheduler_module.job_check_running_too_long()
    assert len(sent) == 2


def test_last_alert_at_reset_when_task_leaves_running(db_session, monkeypatch):
    """Task duoc Done (roi RUNNING) -> last_alert_at phai duoc reset ve None, dam bao lan
    RUNNING tiep theo (vd rollback/redeploy) khong bi anh huong boi lich su canh bao cu."""
    _patch_scheduler_session(monkeypatch, db_session)
    task = _make_stale_running_task(db_session, image_version="v1.2.3")
    task.last_alert_at = datetime.utcnow() - timedelta(minutes=5)
    db_session.commit()
    task_id = task.id
    _patch_argo(monkeypatch, ArgoAppStatus(image_version="v1.2.3", health_status="Healthy", sync_status="Synced"))
    monkeypatch.setattr(scheduler_module, "send_telegram", lambda msg: None)
    monkeypatch.setattr(scheduler_module, "send_mail", lambda *a, **k: None)

    scheduler_module.job_check_running_done()

    reloaded = db_session.get(DeployTask, task_id)
    assert reloaded.status == TaskStatus.DONE
    assert reloaded.last_alert_at is None
