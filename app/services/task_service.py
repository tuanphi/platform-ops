"""Business logic cho vòng đời deploy task: create -> approve/reject -> run."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models import DeployTask, Group, Project, TaskStatus, User
from app.services import git_service
from app.services.app_setting_service import get_app_setting

logger = logging.getLogger(__name__)


@dataclass
class ServiceResult:
    ok: bool
    message: str
    task: DeployTask | None = None


CONFLICT_MESSAGE_TEMPLATE = "Task vừa được thao tác bởi request khác (trạng thái hiện tại: {status}), vui lòng tải lại trang"


def _cas_update(db: Session, task_id: int, status_condition: ColumnElement[bool], values: dict) -> int:
    """Compare-And-Swap: UPDATE nguyên tử `deploy_task` chỉ khi `status_condition` (dựa
    trên status TRONG DATABASE tại thời điểm UPDATE thực thi, không phải giá trị Python
    đã đọc trước đó) vẫn đúng - thay cho pattern "đọc status bằng Python -> if hợp lệ ->
    gán -> commit" vốn có khoảng hở race condition giữa lúc đọc và lúc ghi (2 request
    đồng thời, vd Run và Reject, có thể cùng đọc được status hợp lệ rồi cùng ghi đè
    nhau). Trả về số dòng thực sự khớp WHERE (0 hoặc 1) - 0 nghĩa là 1 request khác đã
    thay đổi status của task này xen giữa lúc caller đọc task và lúc CAS này chạy.

    LƯU Ý QUAN TRỌNG (MySQL/pymysql): mặc định pymysql KHÔNG bật cờ CLIENT_FOUND_ROWS,
    nên `cursor.rowcount` của MySQL phản ánh số dòng THỰC SỰ ĐỔI GIÁ TRỊ, không phải số
    dòng KHỚP WHERE (khác SQLite, luôn trả số dòng khớp WHERE) - nếu `values` không làm
    đổi giá trị nào (vd set_auto_deploy gọi lại với đúng tham số cũ), MySQL sẽ báo
    rowcount=0 dù WHERE có khớp, khiến code hiểu nhầm là có xung đột. Vì vậy luôn ép kèm
    `updated_at` (giá trị luôn mới) vào values để đảm bảo rowcount phản ánh đúng "có
    khớp WHERE hay không" trên mọi backend."""
    payload = dict(values)
    payload.setdefault("updated_at", datetime.utcnow())
    stmt = update(DeployTask).where(DeployTask.id == task_id, status_condition).values(**payload)
    result = db.execute(stmt)
    return result.rowcount


def create_task(
    db: Session,
    user: User,
    group_id: int,
    application: str,
    commitid: str,
    config_env: str | None,
    updatefor: str | None,
    confirmed_run_at: datetime,
) -> ServiceResult:
    if not (updatefor or "").strip():
        return ServiceResult(ok=False, message="Mục đích không được để trống")

    group = db.get(Group, group_id)
    if group is None:
        return ServiceResult(ok=False, message="Group không tồn tại")

    duplicate = (
        db.query(DeployTask)
        .filter(
            DeployTask.project == group.group_name,
            DeployTask.application == application,
            DeployTask.commitid == commitid[:7],
            DeployTask.status.notin_([TaskStatus.CANCELLED, TaskStatus.REJECTED]),
        )
        .first()
    )
    if duplicate is not None:
        return ServiceResult(
            ok=False,
            message=f"Commit này đã có request khác (task #{duplicate.id}, trạng thái {duplicate.status.value})",
        )

    # Bat buoc application phai khop CHINH XAC 1 Project dang active trong catalog cua
    # group nay (giong cach dropdown /api/groups/{id}/projects build) - chan truong hop
    # nguoi dung/bot gui thang application tuy y (khong qua dropdown) de bypass catalog
    # hoac ghep thanh 1 path la nguy hiem khi Run (vd chua "../").
    project_row = (
        db.query(Project)
        .filter(
            Project.group_id == group.id,
            Project.application_name == application,
            Project.is_active.is_(True),
        )
        .first()
    )
    if project_row is None:
        return ServiceResult(
            ok=False,
            message=f"Application '{application}' không tồn tại trong catalog của group '{group.group_name}'",
        )

    try:
        check = git_service.check_commit_on_staging(
            group.group_name, application, commitid, get_app_setting(db).git_remote_repo_url
        )
    except Exception as exc:
        # check_commit_on_staging co the raise GitError (vd staging lock timeout do dang
        # co Run/verify/catalog-sync khac giu lock, hoac git CLI loi) - bat lai o day de
        # tra ve ServiceResult than thien thay vi de exception lan thang ra router thanh
        # HTTP 500. Chua db.add() nen khong co ban ghi DB rac can rollback.
        logger.exception(
            "check_commit_on_staging raise loi ngoai du kien khi create_task (group=%s, application=%s)",
            group.group_name,
            application,
        )
        return ServiceResult(ok=False, message=f"Kiểm tra commit thất bại: {exc}")
    if check.status != "ok":
        return ServiceResult(ok=False, message=check.message)

    task = DeployTask(
        project=group.group_name,
        application=application,
        commitid=commitid[:7],
        config_env=config_env,
        updatefor=updatefor.strip(),
        status=TaskStatus.TASK,
        email=user.email,
        confirmed_run_at=confirmed_run_at,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return ServiceResult(ok=True, message="Tạo yêu cầu thành công!", task=task)


REJECTABLE_STATUSES = (TaskStatus.TASK, TaskStatus.APPROVED)


def reject_task(db: Session, task_id: int, user: User) -> ServiceResult:
    task = db.get(DeployTask, task_id)
    if task is None:
        return ServiceResult(ok=False, message="Task không tồn tại")
    if task.status not in REJECTABLE_STATUSES:
        return ServiceResult(
            ok=False,
            message=f"Task đang ở trạng thái {task.status.value}, không thể Reject!",
            task=task,
        )

    matched = _cas_update(
        db,
        task_id,
        DeployTask.status.in_(REJECTABLE_STATUSES),
        {"status": TaskStatus.REJECTED, "maintainer_confirmed": user.email},
    )
    if matched == 0:
        db.rollback()
        db.refresh(task)
        return ServiceResult(ok=False, message=CONFLICT_MESSAGE_TEMPLATE.format(status=task.status.value), task=task)

    db.commit()
    db.refresh(task)
    return ServiceResult(ok=True, message="Task rejected!", task=task)


CANCELLABLE_STATUSES = tuple(s for s in TaskStatus if s not in (TaskStatus.DONE, TaskStatus.RUNNING))


def cancel_task(db: Session, task_id: int, user: User) -> ServiceResult:
    task = db.get(DeployTask, task_id)
    if task is None:
        return ServiceResult(ok=False, message="Task không tồn tại")
    if task.status in (TaskStatus.DONE, TaskStatus.RUNNING):
        return ServiceResult(ok=False, message="Task is done/running, cannot cancel!", task=task)

    matched = _cas_update(
        db,
        task_id,
        DeployTask.status.in_(CANCELLABLE_STATUSES),
        {"status": TaskStatus.CANCELLED, "maintainer_confirmed": user.email},
    )
    if matched == 0:
        db.rollback()
        db.refresh(task)
        return ServiceResult(ok=False, message=CONFLICT_MESSAGE_TEMPLATE.format(status=task.status.value), task=task)

    db.commit()
    db.refresh(task)
    return ServiceResult(ok=True, message="Task cancelled!", task=task)


def approve_task(db: Session, task_id: int, user: User) -> ServiceResult:
    task = db.get(DeployTask, task_id)
    if task is None:
        return ServiceResult(ok=False, message="Task không tồn tại")
    if task.status != TaskStatus.TASK:
        return ServiceResult(
            ok=False,
            message=f"Task đang ở trạng thái {task.status.value}, không thể Approve!",
            task=task,
        )

    matched = _cas_update(
        db,
        task_id,
        DeployTask.status == TaskStatus.TASK,
        {
            "status": TaskStatus.APPROVED,
            "maintainer_confirmed": user.email,
            "notify_status": 0,
            "confirmed_at": datetime.utcnow(),
        },
    )
    if matched == 0:
        db.rollback()
        db.refresh(task)
        return ServiceResult(ok=False, message=CONFLICT_MESSAGE_TEMPLATE.format(status=task.status.value), task=task)

    db.commit()
    db.refresh(task)
    return ServiceResult(ok=True, message="Data updated successfully!", task=task)


DELETABLE_STATUSES = (TaskStatus.REJECTED, TaskStatus.CANCELLED, TaskStatus.DONE)


def delete_task(db: Session, task_id: int, user: User) -> ServiceResult:
    """Xoa vinh vien (khac Cancel - chi doi status): chi cho phep khi task da ket thuc
    (Rejected/Cancelled/Done), KHONG cho xoa Task/Approved/Running - tranh mat du lieu
    dang xu ly. Phan quyen (chi Super Admin) kiem tra o router qua user.can_delete_task,
    ham nay chi kiem tra dieu kien trang thai."""
    task = db.get(DeployTask, task_id)
    if task is None:
        return ServiceResult(ok=False, message="Task không tồn tại")
    if task.status not in DELETABLE_STATUSES:
        return ServiceResult(
            ok=False,
            message=f"Task đang ở trạng thái {task.status.value}, không thể Xóa!",
            task=task,
        )

    db.delete(task)
    db.commit()
    return ServiceResult(ok=True, message=f"Đã xóa task #{task_id}")


RUNNABLE_STATUSES = (TaskStatus.TASK, TaskStatus.APPROVED)


def _count_running_tasks(db: Session) -> int:
    return db.query(DeployTask).filter(DeployTask.status == TaskStatus.RUNNING).count()


def run_task(db: Session, task_id: int, user: User) -> ServiceResult:
    """Bam Run (don le/bulk/scheduler auto-deploy) KHONG con goi git truc tiep - chi CAS
    task sang QUEUED roi tra loi NGAY, khong giu git lock/thread request (xem
    TaskStatus.QUEUED trong models.py). Worker rieng (process_task_queue, chay dinh ky tu
    APScheduler job_process_task_queue) se lay task ra chay that su khi toi luot, ton trong
    AppSetting.max_concurrent_running_tasks tai THOI DIEM DO (khong con kiem tra gioi han
    o day nua - kiem tra som se vo nghia vi task chua chay ngay)."""
    task = db.get(DeployTask, task_id)
    if task is None:
        return ServiceResult(ok=False, message="Task không tồn tại")
    if task.status not in RUNNABLE_STATUSES:
        return ServiceResult(
            ok=False,
            message=f"Task đang ở trạng thái {task.status.value}, không thể Run!",
            task=task,
        )

    previous_status = task.status
    # queued_from_status luu lai status truoc do de worker revert dung neu git that bai
    # luc toi luot chay (xem docstring DeployTask.queued_from_status). maintainer_run ghi
    # nhan NGAY nguoi bam Run (khac truoc day chi ghi sau khi push xong) - van la thong tin
    # dung (nguoi YEU CAU Run chinh la nguoi nay, du viec chay that su bi tri hoan).
    matched = _cas_update(
        db,
        task_id,
        DeployTask.status.in_(RUNNABLE_STATUSES),
        {"status": TaskStatus.QUEUED, "queued_from_status": previous_status, "maintainer_run": user.email},
    )
    if matched == 0:
        db.rollback()
        db.refresh(task)
        return ServiceResult(ok=False, message=CONFLICT_MESSAGE_TEMPLATE.format(status=task.status.value), task=task)

    db.commit()
    db.refresh(task)
    return ServiceResult(ok=True, message="Đã đưa vào hàng đợi, task sẽ chạy khi tới lượt.", task=task)


def _revert_or_fail_queued_task(db: Session, task: DeployTask, reason: str) -> None:
    """Goi boi worker (run_queued_task) SAU KHI 1 task da duoc CAS "chiem cho" sang
    RUNNING nhung git that bai (hoac raise exception ngoai du kien) - khac voi ham cu
    _revert_running_reservation (da bo, luon revert ve 1 previous_status Python co san
    trong cung 1 request), o day phai xu ly 2 truong hop KHAC NHAU vi task da ton tai qua
    2 buoc CAS cach nhau (X -> QUEUED -> RUNNING), khong con giu previous_status trong bo
    nho Python nua:
      - Task Run binh thuong (rollback_target_task_id is None): tra ve dung
        queued_from_status da luu san tren DB tu luc CAS sang QUEUED (Task/Approved), cho
        nguoi dung Run lai duoc - giu dung hanh vi cu "khong doi trang thai khi Run loi".
      - Task Rollback (rollback_target_task_id khac None, dong MOI duoc INSERT rieng cho
        lan rollback nay, khong co "trang thai truoc do" hop ly de quay ve): danh dau
        REJECTED kem ly do that bai noi vao updatefor - khac luong dong bo cu (khong tao
        dong DB neu perform_rollback that bai), o day dong DA ton tai tu luc enqueue nen
        can 1 trang thai ket thuc ro rang thay vi "bien mat" khoi UI.
    Dung CAS (WHERE status=RUNNING) thay vi gan truc tiep de an toan neu 1 tien trinh khac
    (vd scheduler job_check_running_done) vua kip chuyen task sang DONE truoc do."""
    try:
        if task.rollback_target_task_id is not None:
            note = f"{task.updatefor or ''} - Rollback thất bại: {reason}".strip(" -")
            reverted = _cas_update(
                db,
                task.id,
                DeployTask.status == TaskStatus.RUNNING,
                {"status": TaskStatus.REJECTED, "updatefor": note, "last_alert_at": None},
            )
        else:
            fallback_status = task.queued_from_status or TaskStatus.TASK
            reverted = _cas_update(
                db,
                task.id,
                DeployTask.status == TaskStatus.RUNNING,
                {"status": fallback_status, "queued_from_status": None, "last_alert_at": None},
            )
        db.commit()
        if reverted == 0:
            logger.warning(
                "Khong revert/danh dau that bai duoc task #%s sau khi worker Run/Rollback "
                "that bai - status co the da bi 1 tien trinh khac thay doi truoc do",
                task.id,
            )
    except Exception:
        logger.exception("Loi khi revert/danh dau that bai cho task #%s sau khi worker Run/Rollback that bai", task.id)
        db.rollback()


def run_queued_task(
    db: Session, task_id: int, on_running: Callable[[DeployTask], None] | None = None
) -> ServiceResult:
    """Worker: CAS 1 task QUEUED -> RUNNING roi thuc su goi git (perform_rollback neu
    rollback_target_task_id khac None, nguoc lai perform_run) voi background=True (timeout
    lock DAI hon - xem git_service). CHI duoc goi boi process_task_queue (da tu gioi han so
    luong task lay ra theo AppSetting.max_concurrent_running_tasks TRUOC khi goi ham nay -
    xem docstring process_task_queue), khong tu kiem tra lai gioi han o day.

    `on_running`: callback TUY CHON, duoc goi NGAY sau khi CAS QUEUED -> RUNNING thanh cong
    va COMMIT xong, TRUOC khi goi git that su - de caller (vd scheduler.py) gui thong bao
    "Task is Running" dung luc task THAT SU bat dau chay (khac hanh vi cu: gui sau khi CA
    VONG LAP xu ly xong, khien thong bao den tre/sai thu tu - xem job_process_task_queue).
    Loi tu callback nay (vd Telegram cham/loi mang) duoc bat lai va CHI log, KHONG duoc lam
    hong hoac chan viec chay git tiep theo - dung phu thuoc cung notify_service ngay tai day
    (giu tinh than "khong phu thuoc notify_service de de test" cua process_task_queue, xem
    docstring ham do) - caller tu quyet dinh callback lam gi."""
    task = db.get(DeployTask, task_id)
    if task is None:
        return ServiceResult(ok=False, message="Task không tồn tại")

    reserved = _cas_update(db, task_id, DeployTask.status == TaskStatus.QUEUED, {"status": TaskStatus.RUNNING})
    if reserved == 0:
        db.rollback()
        db.refresh(task)
        return ServiceResult(
            ok=False, message="Task không còn ở trạng thái Queued (đã bị thao tác khác)", task=task
        )
    db.commit()
    db.refresh(task)

    if on_running is not None:
        try:
            on_running(task)
        except Exception:
            # Callback (thuong la gui Telegram/mang) khong duoc phep chan hoac lam hong
            # viec chay git ngay sau day - chi log roi tiep tuc.
            logger.exception(
                "run_queued_task: callback on_running raise loi ngoai du kien cho task #%s - bo qua, tiep tuc chay git",
                task_id,
            )

    app_setting = get_app_setting(db)
    config = git_service.ChartPushConfig(
        chart_values_file_template=app_setting.chart_values_file_template,
        git_commit_author_email=app_setting.git_commit_author_email,
        git_commit_author_name=app_setting.git_commit_author_name,
        git_remote_repo_url=app_setting.git_remote_repo_url,
    )
    run_by_email = task.maintainer_run or task.email
    is_rollback = task.rollback_target_task_id is not None

    try:
        if is_rollback:
            result = git_service.perform_rollback(
                task.project, task.application, task.image_version, run_by_email, config, background=True
            )
        else:
            result = git_service.perform_run(
                task.project, task.application, task.commitid, run_by_email, config, background=True
            )
    except Exception as exc:
        logger.exception(
            "Worker: perform_%s raise loi ngoai du kien cho task #%s",
            "rollback" if is_rollback else "run",
            task_id,
        )
        _revert_or_fail_queued_task(db, task, str(exc))
        db.refresh(task)
        return ServiceResult(ok=False, message=f"Chạy thất bại: {exc}", task=task)

    if not result.ok:
        _revert_or_fail_queued_task(db, task, result.message)
        db.refresh(task)
        return ServiceResult(ok=False, message=result.message, task=task)

    task.run_at = datetime.utcnow()
    if not is_rollback:
        task.image_version = result.image_version
    task.queued_from_status = None
    # status da duoc chuyen sang RUNNING tu buoc "chiem cho" o tren, khong can gan lai.
    db.commit()
    db.refresh(task)
    return ServiceResult(ok=True, message=result.message, task=task)


# So giay toi thieu 1 task phai o RUNNING lien tuc TRUOC khi duoc coi la "treo" luc app
# khoi dong (xem recover_stuck_running_tasks) - giam (KHONG loai bo) rui ro hiem gap:
# app tu restart (vd rolling update K8s) dung luc worker (run_queued_task) VUA CAS
# QUEUED->RUNNING va dang goi git that (con vai giay/chuc giay), process CU van chua bi
# kill han. Task RUNNING duoi nguong nay bi BO QUA o lan quet nay, se duoc xet lai o LAN
# KHOI DONG KE TIEP neu van con ket that.
_STARTUP_RECOVERY_MIN_STUCK_SECONDS = 120


def recover_stuck_running_tasks(db: Session) -> list[DeployTask]:
    """Goi 1 LAN DUY NHAT luc app khoi dong (xem app/main.py::lifespan - PHAI goi TRUOC
    start_scheduler(), luc job_process_task_queue chua duoc dang ky nen chac chan khong co
    task nao dang duoc CHINH WORKER NAY xu ly) de phuc hoi cac task bi ket vinh vien o
    TaskStatus.RUNNING do app CRASH/bi kill dung luc worker (run_queued_task) da CAS
    QUEUED->RUNNING nhung chua kip ghi ket qua (dang goi git_service.perform_run/
    perform_rollback, hoac raise exception ma khong toi duoc _revert_or_fail_queued_task).
    Khong co co che tu dong nao khac sua trang thai nay - job_check_running_too_long
    (scheduler.py) chi gui canh bao Telegram, KHONG doi status.

    ===== GIA DINH BAT BUOC - DOC KY TRUOC KHI DOI HA TANG TRIEN KHAI =====
    Ham nay CHI an toan neu app luon chay DUNG 1 process/1 replica tai MOI thoi diem (xem
    comment app/routers/actions_router.py dong ~51-53: bin/run KHONG truyen --workers cho
    uvicorn - hien tai repo nay KHONG co san file Deployment K8s de tu xac nhan replicas=1
    o tang ha tang, gia dinh nay chi dua tren cach uvicorn duoc khoi chay, CAN DevOps xac
    nhan lai Deployment/HPA thuc te truoc khi tin tuong hoan toan). NEU sau nay tang
    replicas > 1 (hoac chay nhieu instance app song song vi bat ky ly do gi), ham nay se
    RAT NGUY HIEM: luc 1 replica MOI khoi dong (deploy/scale/restart) co the "cuop" 1 task
    ma 1 replica KHAC dang thuc su chay (goi git that), khien task bi chuyen SAI trang
    thai trong khi git van dang chay ngam o replica kia - du lieu task va thuc te trien
    khai se lech nhau, co the gay nham lan nghiem trong (vd nguoi dung bam Run lai trong
    khi ban Run truoc do van dang chay that). TRUOC khi tang replicas PHAI thay co che nay
    bang giai phap an toan da-process (vd DB advisory lock/leader election chi 1 replica
    duoc phep quet phuc hoi luc khoi dong) hoac bo han co che nay va xu ly thu cong.

    De giam (KHONG loai bo hoan toan) rui ro overlap ngan han ngay ca khi CHI co 1 replica
    (vd rolling restart - xem _STARTUP_RECOVERY_MIN_STUCK_SECONDS), CHI phuc hoi task da o
    RUNNING lien tuc it nhat nguong do, tinh tu `updated_at` (la thoi diem CAS QUEUED->
    RUNNING, vi _cas_update luon ep updated_at moi vao MOI lan CAS - xem run_queued_task).

    ===== PHUC HOI VE TRANG THAI NAO =====
    KHONG dua ve lai QUEUED de worker tu dong chay lai - RAT NGUY HIEM vi task co the DA
    push git THANH CONG roi moi crash (chua kip ghi DB set run_at/image_version), chay lai
    se PUSH LAN THU 2 len production/staging. An toan hon la dua ve trang thai CHO NGUOI
    DUNG tu kiem tra (ArgoCD/git history) roi xac nhan Run lai THU CONG neu can:
      - Task Run binh thuong (rollback_target_task_id is None): dua ve queued_from_status
        da luu san tren DB (Task hoac Approved) - giong cach _revert_or_fail_queued_task xu
        ly khi git tra loi that bai o luong binh thuong, chi khac ly do (nghi crash thay vi
        git that bai ro rang).
      - Task Rollback (rollback_target_task_id khac None): danh dau REJECTED kem ly do noi
        vao updatefor - giong _revert_or_fail_queued_task, vi dong nay (INSERT rieng cho
        lan rollback) khong co "trang thai truoc do" hop le de quay ve.

    Dung CAS (WHERE status=RUNNING) cho tung task truoc khi ghi - du o gia dinh 1 process
    khong the co tien trinh nao khac doi status xen giua, van giu CAS de nhat quan voi phan
    con lai cua file va lam du phong an toan neu gia dinh do sai."""
    threshold = datetime.utcnow() - timedelta(seconds=_STARTUP_RECOVERY_MIN_STUCK_SECONDS)
    stuck_ids = [
        row[0]
        for row in db.query(DeployTask.id)
        .filter(DeployTask.status == TaskStatus.RUNNING, DeployTask.updated_at < threshold)
        .all()
    ]

    recovered: list[DeployTask] = []
    for task_id in stuck_ids:
        task = db.get(DeployTask, task_id)
        if task is None or task.status != TaskStatus.RUNNING:
            continue

        try:
            if task.rollback_target_task_id is not None:
                note = (
                    f"{task.updatefor or ''} - Tự động phục hồi lúc app khởi động: "
                    "task kẹt ở Running (nghi ngờ app crash/restart lúc đang chạy git), "
                    "KHÔNG tự động chạy lại vì có thể đã push git thành công"
                ).strip(" -")
                matched = _cas_update(
                    db,
                    task.id,
                    DeployTask.status == TaskStatus.RUNNING,
                    {"status": TaskStatus.REJECTED, "updatefor": note, "last_alert_at": None},
                )
            else:
                fallback_status = task.queued_from_status or TaskStatus.TASK
                matched = _cas_update(
                    db,
                    task.id,
                    DeployTask.status == TaskStatus.RUNNING,
                    {"status": fallback_status, "queued_from_status": None, "last_alert_at": None},
                )
            db.commit()
        except Exception:
            logger.exception("Loi khi phuc hoi task #%s ket o Running luc app khoi dong", task_id)
            db.rollback()
            continue

        if matched == 0:
            logger.warning(
                "Khong phuc hoi duoc task #%s luc khoi dong (status khong con la Running khi CAS - "
                "khong nen xay ra o gia dinh 1 process, xem docstring recover_stuck_running_tasks)",
                task_id,
            )
            continue

        db.refresh(task)
        logger.warning(
            "Da phuc hoi task #%s (%s/%s) tu Running ve %s luc app khoi dong - nghi ngo ket do app "
            "crash/restart dang luc chay git (da RUNNING > %ss khong doi status) - CAN KIEM TRA THU "
            "CONG git/ArgoCD truoc khi Run lai",
            task.id,
            task.project,
            task.application,
            task.status.value,
            _STARTUP_RECOVERY_MIN_STUCK_SECONDS,
        )
        recovered.append(task)

    return recovered


def _promote_queue_candidate_ids(db: Session, limit: int) -> list[int]:
    """Tra ve id cac task QUEUED (FIFO theo id, cu nhat truoc) duoc phep worker lay ra
    chay NGAY BAY GIO, ton trong AppSetting.max_concurrent_running_tasks (limit <= 0 =
    khong gioi han). CHI goi tu process_task_queue - chay tuan tu trong 1 job APScheduler
    (max_instances=1, coalesce=True, xem app/scheduler.py) nen khong co race giua nhieu
    lan goi ham nay voi chinh no."""
    query = db.query(DeployTask.id).filter(DeployTask.status == TaskStatus.QUEUED).order_by(DeployTask.id.asc())
    if limit > 0:
        available = max(0, limit - _count_running_tasks(db))
        if available == 0:
            return []
        query = query.limit(available)
    return [row[0] for row in query.all()]


def process_task_queue(
    db: Session, on_running: Callable[[DeployTask], None] | None = None
) -> list[tuple[DeployTask, ServiceResult]]:
    """Worker chinh cua hang doi (goi tu app/scheduler.py::job_process_task_queue moi vai
    giay) - lay cac task QUEUED cu nhat truoc (FIFO theo id), ton trong
    AppSetting.max_concurrent_running_tasks, roi xu ly TUAN TU (khong song song, dung 1
    vong lap for - moi task xong (thanh cong hay that bai) moi sang task ke tiep) bang
    run_queued_task(). `on_running` (tuy chon) duoc CHUYEN THANG xuong run_queued_task cho
    TUNG task va duoc goi NGAY luc task do CAS QUEUED -> RUNNING thanh cong (TRUOC khi goi
    git that su cho task ke tiep trong vong lap) - de scheduler.py gui thong bao Telegram
    "Task is Running" dung luc task THAT SU bat dau chay, thay vi doi CA VONG LAP nay xong
    moi gui (hanh vi cu, sai thu tu/tre khi co nhieu task xep hang - xem
    app/scheduler.py::job_process_task_queue). Van tra ve day du danh sach (task, result) de
    scheduler.py tu quyet dinh gui canh bao khi that bai (chi biet duoc SAU khi
    run_queued_task tra ve) - ham nay KHONG tu import notify_service, chi nhan callback tu
    ben ngoai (giu tinh than de test doc lap voi notify_service)."""
    limit = get_app_setting(db).max_concurrent_running_tasks
    task_ids = _promote_queue_candidate_ids(db, limit)

    results: list[tuple[DeployTask, ServiceResult]] = []
    for task_id in task_ids:
        try:
            result = run_queued_task(db, task_id, on_running=on_running)
        except Exception:
            logger.exception("process_task_queue: run_queued_task raise loi ngoai du kien cho task #%s", task_id)
            db.rollback()
            continue
        task = db.get(DeployTask, task_id)
        if task is not None:
            results.append((task, result))
    return results


TERMINAL_STATUSES = (TaskStatus.DONE, TaskStatus.RUNNING, TaskStatus.REJECTED, TaskStatus.CANCELLED, TaskStatus.QUEUED)
AUTO_DEPLOY_ELIGIBLE_STATUSES = tuple(s for s in TaskStatus if s not in TERMINAL_STATUSES)


def set_auto_deploy(
    db: Session, task_id: int, user: User, enabled: bool, auto_run_at_utc: datetime | None
) -> ServiceResult:
    """Bat/tat auto deploy - khac voi confirmed_run_at (chi nhac nho), auto_run_at den gio
    se duoc scheduler TU DONG Run that su (xem job_auto_deploy trong scheduler.py)."""
    task = db.get(DeployTask, task_id)
    if task is None:
        return ServiceResult(ok=False, message="Task không tồn tại")
    if task.status in TERMINAL_STATUSES:
        return ServiceResult(ok=False, message="Task đã kết thúc, không thể đặt Auto deploy", task=task)
    if enabled and auto_run_at_utc is None:
        return ServiceResult(ok=False, message="Cần chọn thời gian Auto deploy", task=task)

    matched = _cas_update(
        db,
        task_id,
        DeployTask.status.in_(AUTO_DEPLOY_ELIGIBLE_STATUSES),
        {"auto_deploy": enabled, "auto_run_at": auto_run_at_utc if enabled else None},
    )
    if matched == 0:
        db.rollback()
        db.refresh(task)
        return ServiceResult(ok=False, message="Task đã kết thúc, không thể đặt Auto deploy", task=task)

    db.commit()
    db.refresh(task)
    return ServiceResult(ok=True, message="Đã bật Auto deploy!" if enabled else "Đã tắt Auto deploy!", task=task)


ROLLBACK_CANDIDATES_LIMIT = 5


def list_rollback_candidates(db: Session, task: DeployTask) -> list[DeployTask]:
    """5 ban deploy Done gan nhat (khac task hien tai) cua cung project/application,
    moi nhat truoc, de nguoi dung tu chon dung version muon quay ve - khong tu dong
    doan "ban truoc do" vi co the gay dao qua dao lai giua 2 version khi rollback
    nhieu lan lien tiep."""
    return (
        db.query(DeployTask)
        .filter(
            DeployTask.project == task.project,
            DeployTask.application == task.application,
            DeployTask.status == TaskStatus.DONE,
            DeployTask.id != task.id,
            DeployTask.image_version.isnot(None),
        )
        .order_by(DeployTask.id.desc())
        .limit(ROLLBACK_CANDIDATES_LIMIT)
        .all()
    )


def rollback_task(db: Session, task_id: int, target_task_id: int, user: User) -> ServiceResult:
    """Quay lai image_version cua 1 ban deploy Done cu the do nguoi dung chon - tao 1
    DeployTask moi (status=QUEUED) de theo doi bang dung co che giam sat/ArgoCD hien co,
    khong sua lai task cu. KHONG con goi git truc tiep o day (xem TaskStatus.QUEUED trong
    models.py) - dong moi CHI duoc INSERT voi day du thong tin can thiet de push
    (project/application/image_version da COPY tu `target` + rollback_target_task_id danh
    dau day la 1 yeu cau Rollback) roi tra loi NGAY; worker (task_service.run_queued_task,
    chay tu APScheduler job_process_task_queue) se thuc su goi git_service.perform_rollback
    khi toi luot.

    Vi khong con push git ngay tai day, 2 nguy co RACE CONDITION cu (da ghi nhan trong ban
    vá A-HIGH truoc) gio KHONG con nua:
      - "2 request Rollback dong thoi cung task nguon co the push de len nhau": khong con
        y nghia vi push that su gio do 1 worker DUY NHAT xu ly TUAN TU (max_instances=1,
        xem app/scheduler.py::job_process_task_queue) - khong con 2 lan push xay ra dong
        thoi. Neu 2 dong QUEUED trung target duoc tao (vd spam click, du da co cooldown
        rate-limit rieng o actions_router chan phan lon truong hop), dong xu ly SAU van
        duoc bao ve boi co che phat hien "file khong doi" co san trong _apply_version_and_push.
      - "Gioi han so task chay cung luc chi la best-effort, khong dong duoc": khong con ap
        dung vi rollback_task khong con tu quyet dinh RUNNING nua - gioi han
        (AppSetting.max_concurrent_running_tasks) gio do DUY NHAT process_task_queue kiem
        tra dung 1 cho truoc khi CAS QUEUED -> RUNNING (xem _promote_queue_candidate_ids),
        khong con rai rac o nhieu noi tao task Running nhu truoc."""
    task = db.get(DeployTask, task_id)
    if task is None:
        return ServiceResult(ok=False, message="Task không tồn tại")
    if task.status != TaskStatus.DONE:
        return ServiceResult(ok=False, message="Chỉ rollback được task đã Done", task=task)
    app_setting = get_app_setting(db)
    if not task.is_rollback_window_open(app_setting.rollback_allowed_seconds):
        return ServiceResult(
            ok=False,
            message=f"Task đã Done quá {app_setting.rollback_allowed_seconds} giây, không thể rollback nữa",
            task=task,
        )

    target = db.get(DeployTask, target_task_id)
    if (
        target is None
        or target.project != task.project
        or target.application != task.application
        or target.status != TaskStatus.DONE
        or target.image_version is None
    ):
        return ServiceResult(ok=False, message="Version muốn rollback về không hợp lệ", task=task)
    if target.image_version == task.image_version:
        return ServiceResult(
            ok=False, message="Version này đang là version hiện tại, không có gì để rollback", task=task
        )

    rollback_row = DeployTask(
        project=task.project,
        application=task.application,
        commitid=target.commitid,
        image_version=target.image_version,
        status=TaskStatus.QUEUED,
        email=user.email,
        maintainer_confirmed=user.email,
        maintainer_run=user.email,
        confirmed_run_at=datetime.utcnow(),
        confirmed_at=datetime.utcnow(),
        rollback_target_task_id=target.id,
        updatefor=f"Rollback về version {target.image_version} (task #{target.id}, từ task #{task.id})",
    )
    db.add(rollback_row)
    db.commit()
    db.refresh(rollback_row)
    return ServiceResult(
        ok=True, message="Đã đưa yêu cầu Rollback vào hàng đợi, sẽ chạy khi tới lượt.", task=rollback_row
    )
