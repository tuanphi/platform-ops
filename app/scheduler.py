"""Cron job giám sát tự động: nhắc giờ chạy, đối chiếu ArgoCD -> Done, cảnh báo chạy quá lâu."""

import html
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import get_settings
from app.database import SessionLocal
from app.models import DeployTask, TaskStatus
from app.services import catalog_service, production_service, staging_service, task_service
from app.services.app_setting_service import get_app_setting
from app.services.argocd_service import get_running_app_status
from app.services.catalog_service import sync_catalog_from_chart_repo
from app.services.notify_service import build_ascii_table, send_mail, send_telegram, task_summary_lines
from app.timeutil import utc_to_gmt7

AUTO_DEPLOY_EMAIL = "auto-deploy@system"

logger = logging.getLogger(__name__)
settings = get_settings()


def job_remind_approved_now() -> None:
    """Mỗi phút: task Approved đến giờ chạy trong +-3 phút & chưa nhắc lần 2 -> nhắc 'Deploy Now'."""
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        tasks = (
            db.query(DeployTask)
            .filter(
                DeployTask.status == TaskStatus.APPROVED,
                DeployTask.notify_status != 2,
                DeployTask.confirmed_run_at.between(now - timedelta(minutes=3), now + timedelta(minutes=3)),
            )
            .order_by(DeployTask.confirmed_run_at.asc())
            .all()
        )
        if not tasks:
            return

        rows = []
        for task in tasks:
            task.notify_status = 2
            task.run_at = now
            rows.append(
                {
                    "Project": task.project,
                    "Application": task.application,
                    "CommitID": task.commitid,
                    "Run at (VN)": utc_to_gmt7(task.confirmed_run_at),
                    "Secret": "yes" if task.config_env else "no",
                }
            )
        db.commit()

        table = build_ascii_table(rows)
        link = f"<a href='{settings.app_url}/tasks'>Get more info</a>"
        send_telegram(f"<b>💥 Need Deploy Now 💥</b>\n{table}\n{link}")
    finally:
        db.close()


def job_check_running_done() -> None:
    """Mỗi phút: task Running -> đối chiếu ArgoCD, CHỈ set Done khi VỪA khớp version VỪA
    đang Healthy + Synced thật sự trên ArgoCD (status.health.status=="Healthy" và
    status.sync.status=="Synced" - xem argocd_service.get_running_app_status/ArgoAppStatus.
    is_healthy_and_synced). Trước đây CHỈ so sánh version (status.summary.images) nên có
    thể báo Done SAI khi pod đang ImagePullBackOff/CrashLoopBackOff/Pending hoặc chưa qua
    readiness probe dù manifest đã áp dụng đúng version - xem báo cáo [dev].

    Khi ArgoCD đang TẮT (settings.argocd_url_api rỗng), get_running_app_status trả về None
    -> KHÔNG set Done (giữ nguyên hành vi cũ: môi trường không dùng ArgoCD chưa từng tự
    động Done qua job này, không có gì thay đổi/hỏng thêm)."""
    db = SessionLocal()
    try:
        tasks = db.query(DeployTask).filter(DeployTask.status == TaskStatus.RUNNING).all()
        done_rows = []
        for task in tasks:
            # Phong thu chieu sau: 1 task loi (vd argocd_service tra ve du lieu bat thuong
            # ma khong luong truoc duoc, hoac loi khac ngoai du kien) KHONG duoc lam dung
            # ngang ca vong lap - cac task RUNNING khac van phai duoc doi chieu binh thuong
            # trong cung lot quet, cung tinh than voi process_task_queue (task_service.py).
            # KHONG can db.rollback() o day vi ham nay chi db.commit() 1 lan DUY NHAT sau
            # het vong lap (khong co checkpoint commit rieng tung task) - rollback giua
            # chung se xoa mat ket qua Done cua cac task da xu ly dung truoc do trong cung
            # vong lap.
            try:
                argo_status = get_running_app_status(db, task.project, task.application)
                if argo_status is None or argo_status.image_version != task.image_version:
                    continue
                if not argo_status.is_healthy_and_synced:
                    # Version da khop nhung app chua Healthy/Synced that su - CHUA duoc coi la
                    # Done, cho vong quet ke tiep (job_check_running_too_long se canh bao neu
                    # tinh trang nay keo dai qua lau, xem ham do ben duoi).
                    continue
                task.status = TaskStatus.DONE
                task.done_at = datetime.utcnow()
                # Task roi RUNNING -> reset lich su canh bao, tranh anh huong lan RUNNING sau
                task.last_alert_at = None
                done_rows.append(
                    {
                        "Mail Request": task.email,
                        "Project": task.project,
                        "Application": task.application,
                        "CommitID": task.commitid,
                    }
                )
            except Exception:
                logger.exception(
                    "job_check_running_done: loi ngoai du kien khi xu ly task #%s (%s/%s) - bo qua, tiep tuc cac task con lai",
                    task.id,
                    task.project,
                    task.application,
                )
                continue
        if done_rows:
            db.commit()
            table = build_ascii_table(done_rows)
            link = f"<a href='{settings.app_url}/tasks'>Get more info</a>"
            send_telegram(f"<b>✅ Task done ✅</b>\n{table}\n{link}")
            send_mail(table)
    finally:
        db.close()


def job_check_running_too_long() -> None:
    """Mỗi 5 phút: task Running đã quá N phút (AppSetting.running_alert_after_minutes) mà
    VẪN CHƯA đủ điều kiện Done (xem job_check_running_done) -> cảnh báo, phân biệt 2
    trường hợp để dev/ops biết hướng xử lý đúng:
      - Version ArgoCD còn KHÁC target: cảnh báo "not match version" như trước.
      - Version đã khớp nhưng ArgoCD CHƯA Healthy/Synced (vd đang CrashLoopBackOff/
        ImagePullBackOff/Pending) - cảnh báo RÕ RÀNG khác, kèm health/sync status thật, để
        tránh hiểu nhầm là còn đang chờ đúng version (bản chất là app đang lỗi).

    Giãn cách cảnh báo lặp lại (tránh spam kênh Telegram mỗi 5 phút/lần cho 1 task treo
    lâu): dùng DeployTask.last_alert_at + AppSetting.running_alert_repeat_minutes.
      - last_alert_at is None (chưa từng cảnh báo cho lượt RUNNING hiện tại) -> luôn gửi.
      - Đã cảnh báo rồi: running_alert_repeat_minutes <= 0 nghĩa là CHỈ cảnh báo 1 lần duy
        nhất, không lặp lại nữa cho tới khi task rời RUNNING (last_alert_at reset về None -
        xem models.py::DeployTask.last_alert_at). Ngược lại chỉ gửi lại khi đã đủ khoảng
        cách tối thiểu kể từ last_alert_at.
    Hàm này giờ CÓ mutate/commit DB (ghi last_alert_at) - khác trước đây (chỉ đọc + gửi
    telegram)."""
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        app_setting = get_app_setting(db)
        tasks = db.query(DeployTask).filter(DeployTask.status == TaskStatus.RUNNING).all()
        alerted_any = False
        for task in tasks:
            # Phong thu chieu sau: 1 task loi khong duoc lam dung ngang ca vong lap - cung
            # tinh than voi job_check_running_done o tren/process_task_queue
            # (task_service.py). Khong can db.rollback() khi bat loi vi chi gan attribute
            # tren object Python, commit 1 lan DUY NHAT sau het vong lap (giong
            # job_check_running_done) - loi giua chung khong lam mat cac thay doi da xu ly
            # dung truoc do trong cung vong lap.
            try:
                if not task.run_at:
                    continue
                minutes_running = (now - task.run_at).total_seconds() / 60
                if minutes_running <= app_setting.running_alert_after_minutes:
                    continue

                if task.last_alert_at is not None:
                    if app_setting.running_alert_repeat_minutes <= 0:
                        # 0 = chi canh bao 1 lan duy nhat, da gui roi thi bo qua vinh vien
                        continue
                    minutes_since_last_alert = (now - task.last_alert_at).total_seconds() / 60
                    if minutes_since_last_alert < app_setting.running_alert_repeat_minutes:
                        continue

                argo_status = get_running_app_status(db, task.project, task.application)
                # argo_status co the None (ArgoCD tat/goi API loi) - PHAI kiem tra rieng truoc,
                # KHONG duoc chi dua vao so sanh actual_version == task.image_version (ca 2 co
                # the CUNG la None, gay nham la "da khop" trong khi that ra chua biet gi ca).
                if (
                    argo_status is not None
                    and argo_status.image_version == task.image_version
                    and argo_status.is_healthy_and_synced
                ):
                    # Da khop dieu kien Done - job_check_running_done se xu ly o luot quet ke
                    # tiep (hoac da Done xen giua, task nay se bien mat khoi truy van RUNNING
                    # lan sau), khong can canh bao.
                    continue

                app_name = f"{task.project}-{task.application}{app_setting.argocd_application_postfix}"
                argocd_link = f"<a href='{app_setting.argocd_url_application}{app_name}'>{app_name}</a>"
                actual_version = argo_status.image_version if argo_status is not None else None

                if argo_status is None or actual_version != task.image_version:
                    send_telegram(
                        f"<b>⚠️ Application version not matched after {int(minutes_running)} minutes</b>\n"
                        f"<b>- Mail Request:</b> {html.escape(task.email)}\n"
                        f"<b>- Project:</b> {task.project}\n"
                        f"<b>- Application:</b> {task.application}\n"
                        f"<b>- Target version:</b> {task.image_version}\n"
                        f"<b>- Argocd version:</b> {actual_version}\n"
                        f"<b>- Argocd url:</b> {argocd_link}"
                    )
                else:
                    send_telegram(
                        f"<b>🔥 Version matched but application is not Healthy/Synced after {int(minutes_running)} minutes</b>\n"
                        f"<b>- Mail Request:</b> {html.escape(task.email)}\n"
                        f"<b>- Project:</b> {task.project}\n"
                        f"<b>- Application:</b> {task.application}\n"
                        f"<b>- Target version:</b> {task.image_version}\n"
                        f"<b>- Health status:</b> {html.escape(str(argo_status.health_status))}\n"
                        f"<b>- Sync status:</b> {html.escape(str(argo_status.sync_status))}\n"
                        f"<b>- Argocd url:</b> {argocd_link}"
                    )
                task.last_alert_at = now
                alerted_any = True
            except Exception:
                logger.exception(
                    "job_check_running_too_long: loi ngoai du kien khi xu ly task #%s (%s/%s) - bo qua, tiep tuc cac task con lai",
                    task.id,
                    task.project,
                    task.application,
                )
                continue
        if alerted_any:
            db.commit()
    finally:
        db.close()


def job_remind_30min_before() -> None:
    """(Tuỳ chọn, mặc định tắt qua AppSetting.enable_30min_reminder, sửa được ở
    /settings/general) Task Approved sắp đến giờ chạy trong 30 phút -> nhắc trước."""
    db = SessionLocal()
    try:
        if not get_app_setting(db).enable_30min_reminder:
            return
        now = datetime.utcnow()
        tasks = (
            db.query(DeployTask)
            .filter(
                DeployTask.status == TaskStatus.APPROVED,
                DeployTask.notify_status < 1,
                DeployTask.confirmed_run_at.between(now, now + timedelta(minutes=30)),
            )
            .all()
        )
        if not tasks:
            return
        rows = []
        for task in tasks:
            task.notify_status = 1
            rows.append(
                {
                    "Project": task.project,
                    "Application": task.application,
                    "CommitID": task.commitid,
                    "Run at (VN)": utc_to_gmt7(task.confirmed_run_at),
                }
            )
        db.commit()
        table = build_ascii_table(rows)
        send_telegram(f"<b>⏰ Sắp đến giờ deploy (30 phút)</b>\n{table}")
    finally:
        db.close()


def job_auto_deploy() -> None:
    """Mỗi phút: task bật Auto deploy và đã đến giờ hẹn (auto_run_at <= now) -> ĐƯA VÀO
    HÀNG ĐỢI (task_service.run_task giờ chỉ CAS sang QUEUED, KHÔNG còn gọi git trực tiếp
    tại đây nữa - xem TaskStatus.QUEUED trong models.py) để nhất quán với Run/bulk Run/
    Rollback, tránh job scheduler này giữ git lock/chạy git ngay trên thread của chính nó.
    Worker riêng (job_process_task_queue) sẽ thực sự Run khi tới lượt VÀ gửi thông báo
    Telegram "Task is Running (Auto Deploy)" lúc đó (phân biệt với task Run tay - xem
    scheduler.py::_is_auto_deploy_task, dựa vào maintainer_run == AUTO_DEPLOY_EMAIL do
    run_task ghi lại bên dưới, KHÔNG dùng cột auto_deploy vì cột này bị set False ngay tại
    đây trước khi worker kịp chạy) - ở đây CHỈ còn gửi thông báo khi ĐƯA VÀO HÀNG ĐỢI
    THẤT BẠI (vd task đã bị thao tác sang trạng thái khác trước khi tới giờ hẹn). Chạy 1
    lần rồi tắt Auto, không retry liên tục nếu thất bại - tránh spam thông báo cho 1 lỗi
    lặp lại."""
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        tasks = (
            db.query(DeployTask)
            .filter(
                DeployTask.auto_deploy.is_(True),
                DeployTask.auto_run_at.isnot(None),
                DeployTask.auto_run_at <= now,
                DeployTask.status.notin_(task_service.TERMINAL_STATUSES),
            )
            .all()
        )
        if not tasks:
            return

        system_user = SimpleNamespace(email=AUTO_DEPLOY_EMAIL)
        for task in tasks:
            task.auto_deploy = False
            try:
                result = task_service.run_task(db, task.id, system_user)
                db.commit()
                ok, outcome = result.ok, result.message
            except Exception:
                # Khong de 1 task loi lam vo ca job, khien cac task con lai trong cung
                # luot quet khong duoc xu ly va auto_deploy khong duoc tat.
                logger.exception("Auto deploy (dua vao hang doi) that bai cho task #%s", task.id)
                db.rollback()
                task.auto_deploy = False
                db.commit()
                ok, outcome = False, "exception không xác định, xem log server"

            if not ok:
                send_telegram(
                    "⚠️ <b>Auto Deploy thất bại (đưa vào hàng đợi)</b>\n"
                    + task_summary_lines(task)
                    + f"\n<b>Lỗi:</b> {outcome}"
                )
            # ok=True: KHONG gui "Task is Running" o day nua - worker
            # (job_process_task_queue) se gui khi task THAT SU bat dau chay.
    finally:
        db.close()


def _is_auto_deploy_task(task: DeployTask) -> bool:
    """Phan biet task duoc kich hoat boi auto-deploy (job_auto_deploy) hay nguoi dung bam
    Run/bulk Run/Rollback tay - KHONG dung cot DeployTask.auto_deploy vi cot nay bi
    job_auto_deploy tu tat (set False) NGAY khi dua task vao hang doi (xem job_auto_deploy o
    tren, "Chay 1 lan roi tat Auto"), nen luc worker (job_process_task_queue) chay toi task
    nay thi auto_deploy DA la False mat roi - khong con phan biet duoc nguon goc. Thay vao
    do dung maintainer_run: task_service.run_task luon ghi maintainer_run = email cua nguoi
    (hoac he thong) bam Run luc CAS sang QUEUED va gia tri nay KHONG bi doi lai o cac buoc
    CAS ke tiep (QUEUED -> RUNNING) - job_auto_deploy goi run_task voi
    system_user.email == AUTO_DEPLOY_EMAIL nen day la dau hieu on dinh, con nguyen ven toi
    luc worker chay that su."""
    return task.maintainer_run == AUTO_DEPLOY_EMAIL


def _notify_task_running(task: DeployTask) -> None:
    """Callback truyen cho task_service.process_task_queue(on_running=...) - duoc goi NGAY
    sau khi 1 task CAS QUEUED -> RUNNING thanh cong, TRUOC khi git that su chay cho task do
    (xem task_service.run_queued_task) - sua dung vi tri gui "Task is Running" so voi truoc
    day (gui SAU KHI CA VONG LAP xu ly toan bo hang doi xong, khien noi dung sai thuc te -
    task da Done/that bai roi thong bao moi den, va nhieu task xep hang thi tat ca thong bao
    don don den cung luc sau task CUOI CUNG - xem bao cao [dev] Viec 1).

    task_service.run_queued_task da tu boc try/except quanh callback nay nen loi/cham o day
    (goi mang Telegram) KHONG lam hong hoac chan viec chay git tiep theo - vi du an toan van
    khong bat exception thua o day de tranh 1 loi hi hu lam mat thong bao that bai duoc gui
    o job_process_task_queue ben duoi (2 buoc gui thong bao doc lap nhau)."""
    actor_email = task.maintainer_run or task.email
    title = "🚀 <b>Task is Running (Auto Deploy)</b>\n" if _is_auto_deploy_task(task) else "🚀 <b>Task is Running</b>\n"
    send_telegram(title + task_summary_lines(task, "Run by", actor_email))


def job_process_task_queue() -> None:
    """Moi 5s: worker nen xu ly hang doi Run/Rollback (task o TaskStatus.QUEUED) - xem
    task_service.process_task_queue. Dang ky voi max_instances=1 + coalesce=True (xem
    start_scheduler) de dam bao khong bao gio co 2 luot job nay chay chong nhau (tranh 2
    lan CAS QUEUED->RUNNING xen ke nhau khong can thiet, du CAS tu no da an toan).

    Thong bao "Task is Running" gio duoc gui qua callback _notify_task_running, TRUYEN VAO
    process_task_queue(on_running=...) - duoc goi NGAY luc TUNG task CAS QUEUED->RUNNING
    thanh cong (truoc khi git that su chay cho task do), KHONG con doi CA VONG LAP xu ly het
    hang doi moi gui hang loat nhu truoc (xem docstring _notify_task_running). Thong bao that
    bai van CHI biet duoc SAU KHI tung task chay xong nen van gui o vong lap ben duoi, sau
    khi process_task_queue tra ve."""
    db = SessionLocal()
    try:
        try:
            processed = task_service.process_task_queue(db, on_running=_notify_task_running)
        except Exception:
            logger.exception("job_process_task_queue that bai ngoai du kien")
            return

        for task, result in processed:
            if not result.ok:
                send_telegram(
                    "⚠️ <b>Task execution failed</b>\n" + task_summary_lines(task) + f"\n<b>Lỗi:</b> {result.message}"
                )
    finally:
        db.close()


def job_auto_sync_catalog() -> None:
    """Moi CATALOG_AUTO_SYNC_INTERVAL_MINUTES phut: neu da bat Auto sync qua UI /catalog,
    tu dong chay lai sync_catalog_from_chart_repo - khong can bam nut Sync thu cong moi lan
    cau truc chart thay doi. Job van luon dang ky, tu kiem tra co bat hay khong ben trong,
    giong cach job_auto_deploy loc tung task theo cot auto_deploy."""
    db = SessionLocal()
    try:
        setting = catalog_service.get_catalog_setting(db)
        if not setting.auto_sync_enabled:
            return

        try:
            summary = sync_catalog_from_chart_repo(db)
        except Exception:
            logger.exception("Auto sync catalog that bai")
            return

        if any(summary.values()):
            logger.info(
                "Auto sync catalog: tao moi %s group, %s application - xoa %s group thua, %s application thua",
                summary["created_groups"],
                summary["created_projects"],
                summary["deleted_groups"],
                summary["deleted_projects"],
            )
    finally:
        db.close()


def job_sync_env_apps() -> None:
    """Moi CATALOG_AUTO_SYNC_INTERVAL_MINUTES phut: dong bo lai cache EnvAppCache cho ca
    Staging (nhanh staging) va Production (nhanh master) tu git - GIONG job_auto_sync_catalog
    nhung KHONG co cong tac rieng bat/tat (luon chay khi enable_scheduler dang bat), vi
    tinh nang Bat/Tat staging/Production can danh sach app luon kha moi de nguoi dung
    khong thao tac nham tren du lieu qua cu. Loi tung ben (staging/production) duoc bat
    doc lap - 1 ben loi (vd repo chua cau hinh) khong lam ben con lai khong duoc dong bo."""
    db = SessionLocal()
    try:
        try:
            staging_service.sync_apps(db)
        except Exception:
            logger.exception("Auto sync staging app cache that bai")

        try:
            production_service.sync_apps(db)
        except Exception:
            logger.exception("Auto sync production app cache that bai")
    finally:
        db.close()


scheduler = BackgroundScheduler(timezone="UTC")

# Cac job PHU thuoc cong tac AppSetting.enable_scheduler (Setting > General) - KHONG bao
# gom "process_task_queue": worker xu ly hang doi Run/Rollback la chuc nang cot loi cua
# app (run_task/rollback gio chi enqueue QUEUED, job nay moi thuc su chay git - xem
# task_service.process_task_queue), phai LUON chay bat ke cong tac nay bat/tat, neu khong
# task se ket vinh vien o Queued khi admin tat scheduler ma khong biet.
_OPTIONAL_JOB_IDS = [
    "remind_approved_now",
    "check_running_done",
    "check_running_too_long",
    "auto_deploy",
    "auto_sync_catalog",
    "remind_30min_before",
    "sync_env_apps",
]


def _add_optional_jobs() -> None:
    """Dang ky cac job PHU (nhac gio, doi chieu ArgoCD, canh bao, auto deploy, auto sync
    catalog) - chi goi khi AppSetting.enable_scheduler dang bat."""
    scheduler.add_job(job_remind_approved_now, "cron", minute="*", id="remind_approved_now", replace_existing=True)
    scheduler.add_job(job_check_running_done, "cron", minute="*", id="check_running_done", replace_existing=True)
    scheduler.add_job(job_check_running_too_long, "cron", minute="*/10", id="check_running_too_long", replace_existing=True)
    scheduler.add_job(job_auto_deploy, "cron", minute="*", id="auto_deploy", replace_existing=True)
    scheduler.add_job(
        job_auto_sync_catalog,
        "interval",
        minutes=settings.catalog_auto_sync_interval_minutes,
        id="auto_sync_catalog",
        replace_existing=True,
    )
    scheduler.add_job(
        job_remind_30min_before, "cron", minute="*/5", id="remind_30min_before", replace_existing=True
    )
    scheduler.add_job(
        job_sync_env_apps,
        "interval",
        minutes=settings.catalog_auto_sync_interval_minutes,
        id="sync_env_apps",
        replace_existing=True,
    )


def _remove_optional_jobs() -> None:
    """Go het cac job PHU (neu dang co) - process_task_queue KHONG bi dung theo."""
    for job_id in _OPTIONAL_JOB_IDS:
        if scheduler.get_job(job_id) is not None:
            scheduler.remove_job(job_id)


def apply_scheduler_setting() -> None:
    """Doc lai AppSetting.enable_scheduler tu DB va dong bo cac job PHU cho khop (them
    neu vua bat, go neu vua tat) - goi duoc nhieu lan, dung khi nut bat/tat o UI
    /settings/general duoc bam, khong dung ham nay de dung/khoi dong ca scheduler vi
    process_task_queue phai luon chay (xem _OPTIONAL_JOB_IDS)."""
    if not scheduler.running:
        # Loi im lang nguy hiem: neu scheduler bi dung ngoai y muon (vd loi start()
        # truoc do) ma app van phuc vu request binh thuong, admin bam bat/tat cong tac
        # tren UI van thay "Da luu" thanh cong (gia tri DB dung) nhung KHONG co job PHU
        # nao thuc su duoc them/go (queue worker process_task_queue cung dang KHONG chay -
        # Run/Rollback se ket vinh vien o Queued) - log WARNING de phat hien som qua log/
        # alerting, tranh im lang hoan toan nhu truoc.
        logger.warning(
            "apply_scheduler_setting() duoc goi nhung scheduler HIEN KHONG CHAY - setting "
            "enable_scheduler vua luu tu UI /settings/general SE KHONG co hieu luc (khong "
            "job nao duoc them/go), va worker xu ly hang doi Run/Rollback (process_task_queue) "
            "cung dang KHONG chay - can kiem tra ngay ly do scheduler dung (restart app hoac "
            "xem log loi luc start_scheduler())"
        )
        return
    if get_app_setting().enable_scheduler:
        _add_optional_jobs()
    else:
        logger.info(
            "Cac job phu (nhac gio, ArgoCD, canh bao, auto deploy, auto sync catalog) "
            "dang tat qua cau hinh (Setting > General) - worker xu ly hang doi Run/Rollback van chay binh thuong"
        )
        _remove_optional_jobs()


def start_scheduler() -> None:
    """Luon khoi dong BackgroundScheduler va dang ky job_process_task_queue (worker xu ly
    hang doi Run/Rollback) - day la chuc nang cot loi, KHONG duoc phep bi tat cung
    AppSetting.enable_scheduler (xem _OPTIONAL_JOB_IDS). Cac job PHU con lai chi duoc them
    khi enable_scheduler dang bat (doc tu DB, sua duoc tu UI /settings/general ma khong
    can restart app - xem apply_scheduler_setting). Tu kiem tra scheduler.running de tranh
    goi start() 2 lan."""
    if scheduler.running:
        apply_scheduler_setting()
        return

    # max_instances=1 + coalesce=True BAT BUOC (xem docstring job_process_task_queue) -
    # khong duoc de 2 luot job nay chong nhau.
    scheduler.add_job(
        job_process_task_queue,
        "interval",
        seconds=5,
        id="process_task_queue",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    scheduler.start()
    logger.info("Scheduler đã khởi động (worker xử lý hàng đợi Run/Rollback luôn bật)")
    apply_scheduler_setting()


def shutdown_scheduler() -> None:
    """Dung TOAN BO scheduler (bao gom ca worker hang doi process_task_queue) - CHI goi
    luc app tat (xem lifespan trong main.py). KHONG goi ham nay khi admin tat cong tac
    enable_scheduler qua UI /settings/general - dung apply_scheduler_setting() cho truong
    hop do, de tranh lam Run/Rollback ket vinh vien o Queued."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
