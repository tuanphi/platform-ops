"""BackgroundScheduler RIENG cho menu Schedule - chay moi phut cac lich Restart/Tat/Bat app
den gio (schedule_service.run_due_schedules). Tach khoi app/scheduler.py de KHONG dung
toi cac job/cong tac enable_scheduler san co: lich do nguoi dung chu dong tao nen luon
chay, khong phu thuoc cong tac Setting > General."""

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.database import SessionLocal
from app.services import schedule_service

logger = logging.getLogger(__name__)

schedule_scheduler = BackgroundScheduler(timezone="UTC")


def job_run_app_schedules() -> None:
    db = SessionLocal()
    try:
        schedule_service.run_due_schedules(db)
    except Exception:
        logger.exception("job_run_app_schedules thất bại")
    finally:
        db.close()


def start_schedule_runner() -> None:
    if schedule_scheduler.running:
        return
    # max_instances=1 + coalesce=True: 1 luot chay git/ArgoCD cham khong duoc chong len
    # luot ke tiep.
    schedule_scheduler.add_job(
        job_run_app_schedules,
        "cron",
        minute="*",
        id="run_app_schedules",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    schedule_scheduler.start()
    logger.info("Schedule runner đã khởi động")


def shutdown_schedule_runner() -> None:
    if schedule_scheduler.running:
        schedule_scheduler.shutdown(wait=False)
