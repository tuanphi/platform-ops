"""Business logic cho menu Schedule (/schedule): hen gio tu dong Restart hoac Tat/Bat app
(replicas 0/1) tren staging/production theo tan suat hang ngay/hang tuan/hang thang/1 lan.

Thuc thi dung LAI dung ham cua nut bam tay (restart_service.restart_app, staging_service/
production_service.toggle_app) - KHONG lap lai logic regex/whitelist/rate-limit/git/
notify - duoi danh nghia nguoi tao lich. Quyen cua nguoi tao duoc kiem LAI luc chay: user
bi khoa/bi thu quyen sau khi tao lich thi lich do se that bai (ghi last_message), khong
chay voi quyen cu.

KHONG kiem tra quyen nguoi xem/tao o tang router - dung can_manage() ben duoi."""

import calendar
import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import case
from sqlalchemy.orm import Session

from app.models import AppSchedule, EnvAppCache, User
from app.services import env_toggle_service, notify_service, production_service, restart_service, staging_service
from app.services.telegram_text import to_english
from app.timeutil import gmt7_to_utc, utc_to_gmt7

logger = logging.getLogger(__name__)

# Production dat TRUOC: thu tu hien thi trong dropdown Moi truong o form tao lich.
ENVS = ("production", "staging")
ACTIONS = {"restart": "Restart", "stop": "Tắt", "start": "Bật"}
FREQUENCIES = {"daily": "Hàng ngày", "weekly": "Hàng tuần", "monthly": "Hàng tháng", "once": "1 lần"}
WEEKDAYS = ["Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6", "Thứ 7", "Chủ nhật"]

# Git dang ban -> thu lai o luot job ke tiep (moi phut).
RETRY_INTERVAL = timedelta(minutes=1)

# Job chay moi phut; neu app bi tat lau hon nguong nay (vd deploy lai/su co) thi lan hen
# da qua KHONG chay bu (tranh tat/restart app vao thoi diem khong ai mong doi) - chi bo qua
# va tinh lan ke tiep.
MISSED_GRACE = timedelta(minutes=10)

_APP_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


@dataclass
class ScheduleResult:
    ok: bool
    message: str
    skipped: bool = False
    retry: bool = False
    """Loi tam thoi (git dang ban) - run_due_schedules giu lich o trang thai den han de thu
    lai o phut sau (toi da MISSED_GRACE)."""


def can_manage(user: User, env: str, action: str) -> bool:
    """Quyen RIENG cua menu Schedule (User.can_schedule) - KHONG ke thua quyen Restart/
    sua replicas cua menu Staging/Production."""
    return user.is_active and env in ENVS and user.can_schedule(env, action)


def visible_envs(user: User, action: str | None = None) -> list[str]:
    """Moi truong user XEM duoc lich loai `action` (quyen View, hoac quyen tao DUNG loai lich
    do - Lich Restart chi mo tab restart, Lich Tat/Bat chi mo tab tat/bat). Tao/sua/xoa van
    kiem rieng bang can_manage()."""
    return [env for env in ENVS if user.is_active and user.can_view_schedule(env, action)]


def compute_next_run(schedule: AppSchedule, after_utc: datetime) -> datetime | None:
    """Lan chay ke tiep (UTC) SAU after_utc, None neu lich 1 lan da qua. Moc gio tinh
    theo GMT+7."""
    if schedule.frequency == "once":
        return schedule.run_once_at if schedule.run_once_at and schedule.run_once_at > after_utc else None

    hour, minute = (int(x) for x in schedule.time_of_day.split(":"))
    local_after = utc_to_gmt7(after_utc)
    base = local_after.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if schedule.frequency == "daily":
        candidate = base if base > local_after else base + timedelta(days=1)
    elif schedule.frequency == "weekly":
        candidate = base + timedelta(days=(schedule.weekday - base.weekday()) % 7)
        if candidate <= local_after:
            candidate += timedelta(days=7)
    elif schedule.frequency == "monthly":
        year, month = base.year, base.month
        while True:
            day = min(schedule.day_of_month, calendar.monthrange(year, month)[1])
            candidate = base.replace(year=year, month=month, day=day)
            if candidate > local_after:
                break
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    else:
        return None
    return gmt7_to_utc(candidate)


def describe(schedule: AppSchedule) -> str:
    """Mo ta tan suat de hien thi tren UI (gio GMT+7)."""
    if schedule.frequency == "once":
        return f"1 lần lúc {utc_to_gmt7(schedule.run_once_at).strftime('%d/%m/%Y %H:%M')}" if schedule.run_once_at else "1 lần"
    if schedule.frequency == "weekly":
        return f"Hàng tuần {WEEKDAYS[schedule.weekday]} lúc {schedule.time_of_day}"
    if schedule.frequency == "monthly":
        return f"Hàng tháng ngày {schedule.day_of_month} lúc {schedule.time_of_day}"
    return f"Hàng ngày lúc {schedule.time_of_day}"


def _build_fields(
    db: Session,
    user: User,
    *,
    action: str,
    env: str,
    project: str,
    application: str,
    frequency: str,
    time_of_day: str = "",
    weekday: int | None = None,
    day_of_month: int | None = None,
    run_once_at_local: datetime | None = None,
    now: datetime,
    exclude_id: int | None = None,
) -> tuple[dict | None, str | None]:
    """Validate du lieu form tao/sua lich - tra ve (cac truong cua AppSchedule, None) hoac
    (None, thong bao loi). Dung CHUNG cho create_schedule va update_schedule."""
    if action not in ACTIONS or env not in ENVS or frequency not in FREQUENCIES:
        return None, "Dữ liệu không hợp lệ"
    if not can_manage(user, env, action):
        return None, "Bạn không có quyền với lịch này"
    if not _APP_NAME_RE.fullmatch(project) or not _APP_NAME_RE.fullmatch(application):
        return None, "Tên project/application không hợp lệ"
    if db.query(EnvAppCache).filter_by(env=env, project=project, application=application).first() is None:
        return None, "Ứng dụng không tồn tại trong danh sách được phép"

    fields = {
        "action": action,
        "env": env,
        "project": project,
        "application": application,
        "frequency": frequency,
        "time_of_day": None,
        "weekday": None,
        "day_of_month": None,
        "run_once_at": None,
    }
    if frequency == "once":
        if run_once_at_local is None:
            return None, "Vui lòng chọn ngày giờ chạy"
        fields["run_once_at"] = gmt7_to_utc(run_once_at_local.replace(second=0, microsecond=0))
        if fields["run_once_at"] <= now:
            return None, "Thời điểm chạy phải ở tương lai"
    else:
        if not _TIME_RE.fullmatch(time_of_day or ""):
            return None, "Giờ chạy không hợp lệ (HH:MM)"
        fields["time_of_day"] = time_of_day
        if frequency == "weekly":
            if weekday is None or not 0 <= weekday <= 6:
                return None, "Vui lòng chọn thứ trong tuần"
            fields["weekday"] = weekday
        if frequency == "monthly":
            if day_of_month is None or not 1 <= day_of_month <= 31:
                return None, "Ngày trong tháng phải từ 1 đến 31"
            fields["day_of_month"] = day_of_month

    duplicate_query = db.query(AppSchedule).filter_by(is_active=True, **fields)
    if exclude_id is not None:
        duplicate_query = duplicate_query.filter(AppSchedule.id != exclude_id)
    if duplicate_query.first() is not None:
        return None, "Lịch này đã tồn tại"
    return fields, None


MAX_APPS_PER_CREATE = 200


def create_schedule(db: Session, user: User, *, project: str, application: str, now: datetime | None = None, **form) -> ScheduleResult:
    return create_schedules(db, user, [(project, application)], now=now, **form)


def create_schedules(
    db: Session, user: User, apps: list[tuple[str, str]], *, now: datetime | None = None, **form
) -> ScheduleResult:
    """Tao CUNG 1 lich (hanh dong/moi truong/tan suat/gio) cho NHIEU app 1 luc - moi app 1
    dong AppSchedule rieng (sua/tam dung/xoa van theo tung app). Validate tung app bang
    _build_fields; app loi (khong ton tai/trung lich...) bi bo qua va liet ke trong thong
    bao, cac app con lai van duoc tao."""
    now = now or datetime.utcnow()
    apps = list(dict.fromkeys(apps))
    if not apps:
        return ScheduleResult(False, "Vui lòng chọn ít nhất 1 application")
    if len(apps) > MAX_APPS_PER_CREATE:
        return ScheduleResult(False, f"Chỉ được chọn tối đa {MAX_APPS_PER_CREATE} application mỗi lần")

    created: list[AppSchedule] = []
    failed: list[str] = []
    for project, application in apps:
        fields, error = _build_fields(db, user, project=project, application=application, now=now, **form)
        if error:
            failed.append(f"{project}/{application}: {error}")
            continue
        schedule = AppSchedule(**fields, is_active=True, created_by_id=user.id, created_by_email=user.email)
        schedule.next_run_at = compute_next_run(schedule, now)
        db.add(schedule)
        db.flush()  # de check trung lich (_build_fields) thay ca cac app vua them trong batch
        created.append(schedule)
    db.commit()

    if not created:
        if len(failed) == 1:
            return ScheduleResult(False, failed[0].split(": ", 1)[1])
        return ScheduleResult(False, "Không tạo được lịch nào. " + "; ".join(failed))

    first = created[0]
    next_local = utc_to_gmt7(first.next_run_at).strftime("%d/%m/%Y %H:%M")
    target = f"{first.project}/{first.application}" if len(created) == 1 else f"{len(created)} application"
    message = f"Đã tạo lịch {ACTIONS[first.action]} {target} - lần chạy tới: {next_local}"
    if failed:
        message += f". Bỏ qua {len(failed)}: " + "; ".join(failed)
    return ScheduleResult(not failed, message)


def update_schedule(
    db: Session, user: User, schedule_id: int, *, now: datetime | None = None, **form
) -> ScheduleResult:
    """Sua lich da tao - phai co quyen voi CA lich cu (env/action hien tai) LAN gia tri moi.
    Sua xong lich duoc bat lai (is_active=True) va tinh lai next_run_at; giu nguyen nguoi
    tao + lich su lan chay gan nhat."""
    now = now or datetime.utcnow()
    schedule = _get_manageable(db, user, schedule_id)
    if schedule is None:
        return ScheduleResult(False, "Không tìm thấy lịch hoặc bạn không có quyền")
    fields, error = _build_fields(db, user, now=now, exclude_id=schedule.id, **form)
    if error:
        return ScheduleResult(False, error)

    for key, value in fields.items():
        setattr(schedule, key, value)
    schedule.is_active = True
    schedule.next_run_at = compute_next_run(schedule, now)
    db.commit()
    next_local = utc_to_gmt7(schedule.next_run_at).strftime("%d/%m/%Y %H:%M")
    return ScheduleResult(
        True, f"Đã cập nhật lịch {ACTIONS[schedule.action]} {schedule.project}/{schedule.application} - lần chạy tới: {next_local}"
    )


def _get_manageable(db: Session, user: User, schedule_id: int) -> AppSchedule | None:
    schedule = db.get(AppSchedule, schedule_id)
    if schedule is None or not can_manage(user, schedule.env, schedule.action):
        return None
    return schedule


def delete_schedule(db: Session, user: User, schedule_id: int) -> ScheduleResult:
    schedule = _get_manageable(db, user, schedule_id)
    if schedule is None:
        return ScheduleResult(False, "Không tìm thấy lịch hoặc bạn không có quyền")
    db.delete(schedule)
    db.commit()
    return ScheduleResult(True, "Đã xoá lịch")


def set_active(db: Session, user: User, schedule_id: int, active: bool, now: datetime | None = None) -> ScheduleResult:
    schedule = _get_manageable(db, user, schedule_id)
    if schedule is None:
        return ScheduleResult(False, "Không tìm thấy lịch hoặc bạn không có quyền")
    if active:
        next_run = compute_next_run(schedule, now or datetime.utcnow())
        if next_run is None:
            return ScheduleResult(False, "Lịch 1 lần đã chạy xong/qua giờ, không thể bật lại - hãy sửa ngày giờ mới")
        schedule.next_run_at = next_run
    schedule.is_active = active
    db.commit()
    return ScheduleResult(True, "Đã bật lịch" if active else "Đã tạm dừng lịch")


def bulk_edit(
    db: Session, user: User, schedule_ids: list[int], *, now: datetime | None = None, **form
) -> ScheduleResult:
    """Sua CUNG luc nhieu lich: ap dung hanh dong/tan suat/gio moi (form) cho tung lich, GIU
    NGUYEN moi truong + app cua tung lich. Moi lich validate rieng (_build_fields: quyen,
    app con trong danh sach, trung lich); lich loi bi bo qua va liet ke. Sua xong lich duoc
    bat lai + tinh lai lan chay toi (giong update_schedule)."""
    now = now or datetime.utcnow()
    schedule_ids = list(dict.fromkeys(schedule_ids))
    if not schedule_ids:
        return ScheduleResult(False, "Chưa chọn lịch nào")
    for ignored in ("env", "project", "application"):
        form.pop(ignored, None)

    done = 0
    failed: list[str] = []
    for schedule_id in schedule_ids:
        schedule = _get_manageable(db, user, schedule_id)
        if schedule is None:
            failed.append(f"#{schedule_id}: Không tìm thấy lịch hoặc bạn không có quyền")
            continue
        fields, error = _build_fields(
            db, user, env=schedule.env, project=schedule.project, application=schedule.application,
            now=now, exclude_id=schedule.id, **form,
        )
        if error:
            failed.append(f"{schedule.project}/{schedule.application}: {error}")
            continue
        for field, value in fields.items():
            setattr(schedule, field, value)
        schedule.is_active = True
        schedule.next_run_at = compute_next_run(schedule, now)
        db.flush()  # de check trung lich thay cac lich vua sua trong cung batch
        done += 1
    db.commit()

    message = f"Đã cập nhật {done} lịch"
    if failed:
        message += f". Bỏ qua {len(failed)}: " + "; ".join(failed)
    return ScheduleResult(done > 0 and not failed, message)


BULK_OPS = {"pause": "Tạm dừng", "resume": "Bật lại", "delete": "Xoá"}


def bulk_update(
    db: Session, user: User, schedule_ids: list[int], op: str, now: datetime | None = None
) -> ScheduleResult:
    """Tam dung / bat lai / xoa NHIEU lich 1 luc (o tich chon tren danh sach). Moi lich van
    kiem quyen rieng (_get_manageable) nhu thao tac tung cai; lich khong co quyen/khong ton
    tai/lich 1 lan da qua (khi bat lai) bi bo qua va dem vao thong bao."""
    if op not in BULK_OPS:
        return ScheduleResult(False, "Thao tác không hợp lệ")
    schedule_ids = list(dict.fromkeys(schedule_ids))
    if not schedule_ids:
        return ScheduleResult(False, "Chưa chọn lịch nào")
    now = now or datetime.utcnow()

    done = 0
    skipped: list[str] = []
    for schedule_id in schedule_ids:
        schedule = _get_manageable(db, user, schedule_id)
        if schedule is None:
            skipped.append(f"#{schedule_id}: không tìm thấy hoặc không có quyền")
            continue
        if op == "delete":
            db.delete(schedule)
        elif op == "pause":
            schedule.is_active = False
        else:
            next_run = compute_next_run(schedule, now)
            if next_run is None:
                skipped.append(f"{schedule.project}/{schedule.application}: lịch 1 lần đã chạy xong/qua giờ, hãy sửa ngày giờ mới")
                continue
            schedule.next_run_at = next_run
            schedule.is_active = True
        done += 1
    db.commit()

    message = f"Đã {BULK_OPS[op].lower()} {done} lịch"
    if skipped:
        message += f". Bỏ qua {len(skipped)}: " + "; ".join(skipped)
    return ScheduleResult(done > 0 and not skipped, message)


def is_finished(schedule: AppSchedule, now: datetime | None = None) -> bool:
    """Lich 1 lan da qua thoi diem chay (da chay xong hoac bi bo lo) va dang tat - khong bat
    lai duoc nua (chi sua ngay gio moi hoac xoa)."""
    now = now or datetime.utcnow()
    return (
        schedule.frequency == "once"
        and not schedule.is_active
        and (schedule.run_once_at is None or schedule.run_once_at <= now)
    )


def list_schedules(db: Session, user: User, actions: tuple[str, ...]) -> list[AppSchedule]:
    envs = visible_envs(user, actions[0])
    if not envs:
        return []
    # Thu tu CO DINH: moi truong (theo ENVS - Production truoc) -> project -> application ->
    # lich tao truoc. Tam dung/bat lai/sua KHONG lam dong nhay cho; muon xem theo lan chay
    # toi thi bam tieu de cot de sap xep (client-side).
    env_order = case({env: i for i, env in enumerate(ENVS)}, value=AppSchedule.env, else_=len(ENVS))
    return (
        db.query(AppSchedule)
        .filter(AppSchedule.env.in_(envs), AppSchedule.action.in_(actions))
        .order_by(env_order, AppSchedule.project, AppSchedule.application, AppSchedule.id)
        .all()
    )


def _execute(db: Session, schedule: AppSchedule) -> ScheduleResult:
    user = db.get(User, schedule.created_by_id) if schedule.created_by_id else None
    if user is None or not can_manage(user, schedule.env, schedule.action):
        return ScheduleResult(False, f"Người tạo lịch ({schedule.created_by_email}) không còn quyền thực hiện")

    if schedule.action == "restart":
        result = restart_service.restart_app(db, user, schedule.env, schedule.project, schedule.application)
        _notify_restart(schedule, result.ok, result.message)
        return ScheduleResult(result.ok, result.message)

    return _execute_toggle(db, user, schedule)


def _execute_toggle(db: Session, user: User, schedule: AppSchedule) -> ScheduleResult:
    """Tat/Bat theo lich, BO QUA neu app da o dung trang thai (doc EnvAppCache):
      - Tat: app da tat san -> bo qua.
      - Bat: app dang chay (bao nhieu pod cung vay) -> bo qua, giu nguyen so pod.
    Cache cu hon git ma git bao "khong thay doi" cung tinh la bo qua (EnvActionResult.skipped).
    Bo qua: khong commit, khong gui Telegram. Nho/khoi phuc so pod (tat 3 pod -> bat lai 3
    pod) nam o env_toggle_service, dung chung voi Bat/Tat o /staging, /production."""
    key = {"env": schedule.env, "project": schedule.project, "application": schedule.application}
    cache = db.query(EnvAppCache).filter_by(**key).first()
    current = int(cache.replicas) if cache is not None and str(cache.replicas).isdigit() else None
    service = staging_service if schedule.env == "staging" else production_service

    if schedule.action == "stop":
        if current == 0:
            return ScheduleResult(True, "App đã tắt sẵn, không cần tắt", skipped=True)
        result = service.toggle_app(db, user, schedule.project, schedule.application, False, None, scheduled=True)
        if result.skipped:
            return ScheduleResult(True, "Replicas trên git đã là 0, không cần tắt", skipped=True)
        return ScheduleResult(result.ok, result.message, retry=result.busy)

    if current:
        return ScheduleResult(True, f"App đang chạy {current} pod, không cần bật", skipped=True)
    restored = env_toggle_service.remembered_replicas(db, schedule.env, schedule.project, schedule.application)
    result = service.toggle_app(db, user, schedule.project, schedule.application, True, None, scheduled=True)
    if result.skipped:
        return ScheduleResult(True, "Replicas trên git đã đúng, không cần bật", skipped=True)
    if result.busy:
        return ScheduleResult(False, result.message, retry=True)
    message = result.message
    if result.ok and restored:
        message += f" (khôi phục {restored} pod)"
    return ScheduleResult(result.ok, message)


def _notify_restart(schedule: AppSchedule, ok: bool, message: str) -> None:
    """Restart bam tay khong gui Telegram, nhung chay tu dong thi can bao de nguoi
    van hanh biet (Tat/Bat da tu gui qua env_toggle_service.notify)."""
    icon = "✅" if ok else "⚠️"
    text = "\n".join(
        [
            f"{icon} <b>[Scheduled] Restart {schedule.env}: {html.escape(schedule.project)}/{html.escape(schedule.application)}</b>",
            f"<b>- Scheduled by:</b> {html.escape(schedule.created_by_email)}",
            f"<b>- Result:</b> {html.escape(to_english(message))}",
        ]
    )
    try:
        notify_service.send_infra_toggle_telegram(text, schedule.env)
    except Exception:
        logger.exception("Gửi Telegram cho lịch restart #%s thất bại", schedule.id)


def _verb(action: str) -> str:
    return {"stop": "Turn off", "start": "Turn on"}.get(action, "Restart")


def _notify_gave_up(schedules: list[AppSchedule]) -> None:
    """Lich bi bo qua vi git ban qua lau - cac lan thu lai truoc do khong gui Telegram (xem
    EnvActionResult.busy) nen bao 1 lan o day, GOP theo (env, action) - nhieu app 1 tin."""
    groups: dict[tuple[str, str], list[AppSchedule]] = {}
    for schedule in schedules:
        groups.setdefault((schedule.env, schedule.action), []).append(schedule)
    minutes = int(MISSED_GRACE.total_seconds() // 60)
    for (env, action), members in groups.items():
        target = (
            f"{html.escape(members[0].project)}/{html.escape(members[0].application)}"
            if len(members) == 1 else f"({len(members)} applications)"
        )
        header = [
            f"⚠️ <b>[Scheduled] {_verb(action)} {env}: {target}</b>",
            f"<b>- Scheduled by:</b> {html.escape(', '.join(sorted({m.created_by_email for m in members})))}",
            f"<b>- Result:</b> Git was busy for {minutes} minutes, this run was skipped",
        ]
        items = [f"  • {html.escape(m.project)}/{html.escape(m.application)}" for m in members] if len(members) > 1 else []
        if items:
            header.append("<b>- Applications:</b>")
        env_toggle_service.send_chunked_telegram(env, header, items, [])


def _execute_toggle_group(db: Session, schedules: list[AppSchedule]) -> dict[int, ScheduleResult]:
    """Chay GOP nhieu lich Tat/Bat den han CUNG luc, CUNG (env, action, nguoi tao) bang 1 lan
    git (1 commit/1 MR, qua git_service.set_*_replicas) + 1 tin Telegram (tach nhieu tin neu
    danh sach dai) - thay vi N commit/N tin. Tung app van: bo qua neu da o dung trang thai,
    nho/khoi phuc so pod (dung chung env_toggle_service), ket qua ghi rieng tung lich. Git
    ban -> ca nhom retry (xu ly o run_due_schedules)."""
    from app.services import git_service

    first = schedules[0]
    env, action = first.env, first.action
    user = db.get(User, first.created_by_id) if first.created_by_id else None
    if user is None or not can_manage(user, env, action):
        denied = ScheduleResult(False, f"Người tạo lịch ({first.created_by_email}) không còn quyền thực hiện")
        return {s.id: denied for s in schedules}

    results: dict[int, ScheduleResult] = {}
    targets: list[tuple[AppSchedule, int, bool]] = []  # (lich, so pod dat, co khoi phuc khong)
    for schedule in schedules:
        cache = db.query(EnvAppCache).filter_by(env=env, project=schedule.project, application=schedule.application).first()
        current = int(cache.replicas) if cache is not None and str(cache.replicas).isdigit() else None
        if action == "stop":
            if current == 0:
                results[schedule.id] = ScheduleResult(True, "App đã tắt sẵn, không cần tắt", skipped=True)
                continue
            targets.append((schedule, 0, False))
        else:
            if current:
                results[schedule.id] = ScheduleResult(True, f"App đang chạy {current} pod, không cần bật", skipped=True)
                continue
            remembered = env_toggle_service.remembered_replicas(db, env, schedule.project, schedule.application)
            targets.append((schedule, remembered or 1, bool(remembered)))
    if not targets:
        return results

    service = staging_service if env == "staging" else production_service
    set_fn = git_service.set_staging_replicas if env == "staging" else git_service.set_production_replicas
    entries = [(s.project, s.application, n) for s, n, _ in targets]
    try:
        result = set_fn(entries, user.email, service._push_config(db))
    except git_service.GitLockBusyError as exc:
        for schedule, _, _ in targets:
            results[schedule.id] = ScheduleResult(False, str(exc), retry=True)
        return results
    except git_service.GitError as exc:
        result = git_service.ReplicasToggleResult(ok=False, message=str(exc))

    changed = {tuple(pair) for pair in result.changed}
    if not result.ok and changed:  # git bao file khong thay doi -> tat ca da dung san
        for schedule, _, _ in targets:
            results[schedule.id] = ScheduleResult(True, "Replicas trên git đã đúng, không cần thay đổi", skipped=True)
        return results

    if result.ok and changed:
        if action == "stop":
            env_toggle_service.remember_replicas(db, env, result.changed)  # TRUOC write-through
        else:
            env_toggle_service.forget_replicas(db, env, result.changed)
        if not result.pending_merge_request:
            for schedule, n, _ in targets:
                if (schedule.project, schedule.application) in changed:
                    env_toggle_service.write_through_cache(db, env, ((schedule.project, schedule.application),), n)

    for schedule, n, restored in targets:
        if result.ok and (schedule.project, schedule.application) in changed:
            message = result.message + (f" (khôi phục {n} pod)" if restored else "")
            results[schedule.id] = ScheduleResult(True, message)
        elif result.ok:
            results[schedule.id] = ScheduleResult(False, "Không tìm thấy ứng dụng trong file values trên git")
        else:
            results[schedule.id] = ScheduleResult(False, result.message)

    icon = "✅" if result.ok else "⚠️"
    shown = [(s, n) for s, n, _ in targets if not result.ok or (s.project, s.application) in changed]
    header = [
        f"{icon} <b>[Scheduled] {_verb(action)} {env} ({len(shown)} applications)</b>",
        f"<b>- Performed by:</b> {html.escape(user.email)}",
        f"<b>- Result:</b> {html.escape(to_english(result.message))}",
    ]
    link = env_toggle_service.git_link_line(result)
    if link:
        header.append(link)
    skipped = sum(1 for r in results.values() if r.skipped)
    if skipped:
        header.append(f"<b>- Skipped (already in target state):</b> {skipped}")
    header.append("<b>- Applications:</b>")
    items = [f"  • {html.escape(s.project)}/{html.escape(s.application)} (replicas={n})" for s, n in shown]
    env_toggle_service.send_chunked_telegram(env, header, items, [])
    return results


def _safe(db: Session, schedule_ids: list[int], fn) -> dict[int, ScheduleResult]:
    try:
        return fn()
    except Exception as exc:
        logger.exception("Chạy lịch %s thất bại", schedule_ids)
        db.rollback()
        error = ScheduleResult(False, f"Lỗi không xác định: {exc}")
        return {schedule_id: error for schedule_id in schedule_ids}


def run_due_schedules(db: Session, now: datetime | None = None) -> int:
    """Chay cac lich den gio - goi moi phut tu job_run_app_schedules. 3 buoc:
      1. Day next_run_at sang lan ke tiep VA commit TRUOC khi thuc thi (1 loi giua chung -
         git/ArgoCD treo, app crash - khong lam lich bi chay lap lai).
      2. Thuc thi: Restart chay tung lich; Tat/Bat GOP theo (env, action, nguoi tao) thanh
         1 lan git (_execute_toggle_group) - nhom 1 lich thi chay nhu cu (_execute).
      3. Ghi ket qua tung lich. Git dang ban (retry) -> khoi phuc next_run_at = moc goc de
         phut sau thu lai, toi da MISSED_GRACE; het han thi bao that bai (1 tin Telegram gop).
    Tra ve so lich da xu ly."""
    now = now or datetime.utcnow()
    due = (
        db.query(AppSchedule)
        .filter(AppSchedule.is_active.is_(True), AppSchedule.next_run_at.isnot(None), AppSchedule.next_run_at <= now)
        .order_by(AppSchedule.next_run_at.asc(), AppSchedule.id.asc())
        .all()
    )
    scheduled_for: dict[int, datetime] = {}
    for schedule in due:
        scheduled_for[schedule.id] = schedule.next_run_at
        schedule.next_run_at = compute_next_run(schedule, now)
        if schedule.next_run_at is None:
            schedule.is_active = False
    db.commit()

    results: dict[int, ScheduleResult] = {}
    groups: dict[tuple, list[AppSchedule]] = {}
    for schedule in due:
        if now - scheduled_for[schedule.id] > MISSED_GRACE:
            results[schedule.id] = ScheduleResult(
                False,
                f"Bỏ lỡ lần chạy lúc {utc_to_gmt7(scheduled_for[schedule.id]).strftime('%d/%m/%Y %H:%M')} (hệ thống không chạy đúng giờ)",
            )
        elif schedule.action in ("stop", "start"):
            groups.setdefault((schedule.env, schedule.action, schedule.created_by_id), []).append(schedule)
        else:
            results.update(_safe(db, [schedule.id], lambda s=schedule: {s.id: _execute(db, s)}))
    for members in groups.values():
        ids = [m.id for m in members]
        if len(members) == 1:
            results.update(_safe(db, ids, lambda s=members[0]: {s.id: _execute(db, s)}))
        else:
            results.update(_safe(db, ids, lambda ms=members: _execute_toggle_group(db, ms)))

    gave_up: list[AppSchedule] = []
    for schedule in due:
        result = results[schedule.id]
        if result.retry:
            if now - scheduled_for[schedule.id] + RETRY_INTERVAL <= MISSED_GRACE:
                schedule.next_run_at = scheduled_for[schedule.id]
                schedule.is_active = True
                db.commit()
                logger.info("Lịch #%s: git đang bận, sẽ thử lại sau 1 phút", schedule.id)
                continue
            result = ScheduleResult(
                False, f"Git bận liên tục {int(MISSED_GRACE.total_seconds() // 60)} phút, bỏ qua lần chạy này: {result.message}"
            )
            gave_up.append(schedule)
        schedule.last_run_at = now
        schedule.last_ok = result.ok
        schedule.last_skipped = result.skipped
        schedule.last_message = result.message
        db.commit()
        logger.info("Lịch #%s (%s %s/%s %s): ok=%s %s", schedule.id, schedule.action, schedule.project, schedule.application, schedule.env, result.ok, result.message)
    if gave_up:
        _notify_gave_up(gave_up)
    return len(due)
