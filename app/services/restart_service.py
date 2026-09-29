"""Business logic cho nut "Restart" o /staging va /production - goi
argocd_service.restart_workload() (mutate ArgoCD DAU TIEN cua du an, xem docstring ham do)
roi GHI 1 dong RestartLog du thanh cong hay that bai (AC6). KHONG dung chung logic voi
env_toggle_service.py (Bat/Tat/thay doi replicas) vi Restart khong sua file YAML/git,
khong doi cot `replicas`/badge "Trang thai" cua EnvAppCache.

VA LO HONG BAO MAT ([sec] audit, xem app truoc khi vá o duoi):
  1. WHITELIST: project/application nguoi dung gui len CHI duoc phep neu ton tai trong
     EnvAppCache (env=env_label) - day la danh sach app THAT SU duoc render cho user xem
     tren /staging|/production. Khac voi /toggle (project/application chi la khoa tra cuu
     trong 1 file YAML co dinh, sai thi vo hai), o day 2 gia tri nay duoc ghep thanh
     app_name roi PATCH THANG vao ArgoCD -> mutate ha tang, phai chan truoc khi goi
     argocd_service.restart_workload (KHONG chi dua vao co TOAN CUC can_access_staging/
     can_access_production o tang router).
  2. REGEX: project/application phai khop ^[a-zA-Z0-9._-]+$ (fullmatch - $ trong Python
     con khop truoc 1 "\n" cuoi chuoi neu dung match(), nen dung fullmatch() cho chat) -
     lop phong thu thu 2 (defense in depth) cung voi urllib.parse.quote() trong
     argocd_service.py, tranh ky tu "/"/".." lam lech URL PATH goi ArgoCD du da qua duoc
     whitelist (vd ten trong EnvAppCache bi nhiem ky tu la tu nguon dong bo git khong dang
     tin cay 100%).
  3. RATE LIMIT (theo workload): cooldown theo khoa (env_label, project, application), dung
     CHUNG pattern voi _check_rollback_rate_limit trong app/routers/actions_router.py
     (in-process dict + threading.Lock, du dung vi app chi chay 1 process uvicorn) - tranh
     spam nut Restart (chi bi chan boi JS `confirm()`, bypass duoc neu goi thang API) gay
     rolling restart chong len nhau.
  4. RATE LIMIT (theo user, vong vá 2 - CWE-770 log/DB flooding): 3 lop chan o tren
     (permission o router, regex, whitelist) deu ghi 1 dong RestartLog qua
     record_blocked_attempt()/restart_app() TRUOC KHI toi duoc lop rate-limit #3 (vi #3 chi
     chay sau khi da qua whitelist) - nghia la 1 user DA DANG NHAP nhung KHONG co quyen
     Staging/Production nao, hoac gui project/application sai/khong ton tai, co the spam
     POST /staging|production/restart lien tuc ma khong bi chan boi bat ky rate-limit nao,
     lam phinh bang RestartLog + log warning vo han. Vá bang 1 limiter MOI khoa theo
     user.id, check_restart_attempt_rate_limit() ben duoi - KHAC voi #3 (khoa theo
     env/project/application, vi luc do gia tri nay CHUA duoc xac thuc nen khong dung lam
     khoa tin cay duoc) - goi o NGAY DAU 2 route POST /restart (staging_router.py,
     production_router.py), TRUOC CA buoc check CSRF, va KHONG ghi RestartLog khi bi chan
     o day (chinh la diem mau chot: tranh vong lap ghi-log-vo-han ma lo hong nay tao ra).

Diem 1-3 nam trong restart_app() (dung 1 cho duy nhat, KHONG lap lai o 2 router
staging/production) va deu di qua chung code ghi RestartLog o cuoi ham - moi lan bi chan
(sai regex/khong trong whitelist/qua nhanh) VAN duoc ghi 1 dong RestartLog (ok=False) giong
het cac loi khac, khong can code rieng. Diem 4 (check_restart_attempt_rate_limit) la ham
RIENG, goi truc tiep trong router (KHONG nam trong restart_app()/record_blocked_attempt())
vi phai chan TRUOC CA CSRF-check, con restart_app()/record_blocked_attempt() luon ghi log
nen khong the dung cho lop nay."""

import logging
import re
import threading
import time
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import EnvAppCache, RestartLog, User
from app.services import argocd_service
from app.services.app_setting_service import get_app_setting

logger = logging.getLogger(__name__)

# ^[a-zA-Z0-9._-]+$ - du rong de khop ten project/application thuc te (chu/so/dau cham/
# gach ngang/gach duoi), nhung tu choi "/", khoang trang, ".." dung 1 minh lam ky tu dac
# biet lam lech URL PATH ArgoCD (xem argocd_service.py::quote()). BAT BUOC dung fullmatch()
# (KHONG dung match()) khi ap dung pattern nay - trong Python, "$" con khop truoc 1 ky tu
# "\n" cuoi chuoi neu dung match(), nen 1 input co newline o cuoi (vd "app\n") van lot qua
# match() dung anchor "^...$" nay du khong khop toan bo chuoi.
_APP_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

_INVALID_NAME_MESSAGE = "Tên project/application không hợp lệ"
_NOT_WHITELISTED_MESSAGE = "Ứng dụng không tồn tại trong danh sách được phép"

# Cooldown rieng cho Restart, khoa theo (env_label, project, application) - KHONG dung
# chung dict/lock voi _run_rollback_last_call (actions_router.py, khoa theo (email,
# task_id)) vi khac doi tuong can chan trung lap (restart CUNG 1 app, khong phai CUNG 1
# task Rollback boi CUNG 1 user) - nhung dung LAI DUNG 1 pattern (in-process dict +
# threading.Lock, don dep khi dict qua to). Gia tri cooldown (giay) KHONG con la hang so -
# doc tu AppSetting.restart_cooldown_seconds (sua duoc qua Settings > General > ArgoCD,
# mac dinh 60) MOI LAN goi _check_restart_rate_limit, xem incident 2026-08-18 (yeu cau
# nang mac dinh tu 30 len 60 va cho phep chinh qua UI thay vi hang so cung trong code).
_RESTART_RATE_LIMIT_STATE_MAX_ENTRIES = 5000

_restart_last_call_lock = threading.Lock()
_restart_last_call: dict[tuple[str, str, str], float] = {}


def _check_restart_rate_limit(env_label: str, project: str, application: str, cooldown_seconds: float) -> bool:
    """Tra True neu duoc phep Restart app nay ngay bay gio (va ghi nhan moc cooldown ke
    tiep), False neu app nay (cung env/project/application) vua duoc Restart trong vong
    cooldown_seconds giay gan nhat. Khoa theo (env_label, project, application) - KHONG
    theo user, vi muc tieu la bao ve chinh workload khoi bi rolling-restart chong len
    nhau, bat ke ai bam nut."""
    now = time.monotonic()
    key = (env_label, project, application)
    with _restart_last_call_lock:
        if len(_restart_last_call) > _RESTART_RATE_LIMIT_STATE_MAX_ENTRIES:
            stale_before = now - 3600
            for stale_key in [k for k, ts in _restart_last_call.items() if ts < stale_before]:
                del _restart_last_call[stale_key]

        last_call = _restart_last_call.get(key)
        if last_call is not None and (now - last_call) < cooldown_seconds:
            return False
        _restart_last_call[key] = now
        return True


def _restart_rate_limit_message(cooldown_seconds: float) -> str:
    return f"Vui lòng đợi {int(cooldown_seconds)} giây rồi thử lại"


# Rate limit RIENG theo user.id (vong vá 2, CWE-770) - khac hoan toan _check_restart_rate_limit
# o tren (khoa theo env/project/application, chi ap dung SAU KHI da qua whitelist). Limiter
# nay phai chan duoc ca request voi CSRF sai/project-application khong ton tai (nhung gia tri
# CHUA the tin cay duoc luc nay), nen khoa theo danh tinh nguoi goi (user.id, da xac thuc qua
# session dang nhap) thay vi noi dung request.
#
# Dung fixed-window counter (thay vi 1 moc thoi gian "last call" don le nhu 2 limiter tren)
# vi 1 user THAT co the can Restart NHIEU app KHAC NHAU lien tiep trong vai chuc giay (vd sau
# 1 dot deploy) - 1 cooldown don le se chan oan thao tac hop le nay. Van la in-process dict +
# threading.Lock (du dung, app chi chay 1 process uvicorn), co don dep entry cu tranh dict
# phinh to vo han - CUNG 1 pattern voi 2 limiter tren, chi khac cach luu state (dem theo cua
# so thoi gian thay vi 1 moc "last call").
#
# Nguong: toi da 10 lan goi / 60 giay / user - du thoai mai cho nguoi that bam nut Restart
# nhieu app khac nhau (kha nang toi da ~10 app moi phut la hop ly cho thao tac tay), nhung du
# chat de chan spam tu dong (script goi thang API se bi chan sau 10 request dau tien trong
# cua so 1 phut, thay vi ghi khong gioi han row DB/log nhu truoc khi vá).
_RESTART_ATTEMPT_WINDOW_SECONDS = 60.0
_RESTART_ATTEMPT_MAX_PER_WINDOW = 10
_RESTART_ATTEMPT_STATE_MAX_ENTRIES = 5000

_restart_attempt_lock = threading.Lock()
# user_id -> (thoi diem bat dau cua so hien tai, so lan goi da dem trong cua so do)
_restart_attempt_state: dict[int, tuple[float, int]] = {}


def check_restart_attempt_rate_limit(user_id: int) -> bool:
    """Tra True neu user_id nay con duoc phep GOI route POST /restart ngay bay gio (va tang
    bo dem), False neu user nay da goi qua _RESTART_ATTEMPT_MAX_PER_WINDOW lan trong
    _RESTART_ATTEMPT_WINDOW_SECONDS giay gan nhat - BAT KE noi dung request (CSRF dung/sai,
    project/application ton tai hay khong). Phai duoc goi o NGAY DAU router, TRUOC CA
    check_csrf_token(), va khi tra False thi router KHONG duoc goi record_blocked_attempt()
    (khong ghi RestartLog) - day chinh la diem chan vong lap ghi-log-vo-han cua CWE-770."""
    now = time.monotonic()
    with _restart_attempt_lock:
        if len(_restart_attempt_state) > _RESTART_ATTEMPT_STATE_MAX_ENTRIES:
            stale_before = now - _RESTART_ATTEMPT_WINDOW_SECONDS * 10
            for stale_key, (window_start, _count) in list(_restart_attempt_state.items()):
                if window_start < stale_before:
                    del _restart_attempt_state[stale_key]

        window_start, count = _restart_attempt_state.get(user_id, (now, 0))
        if now - window_start >= _RESTART_ATTEMPT_WINDOW_SECONDS:
            # Cua so cu da het han - mo cua so moi, reset bo dem.
            window_start, count = now, 0

        if count >= _RESTART_ATTEMPT_MAX_PER_WINDOW:
            return False

        _restart_attempt_state[user_id] = (window_start, count + 1)
        return True


RESTART_ATTEMPT_RATE_LIMIT_MESSAGE = "Bạn đang thao tác quá nhanh, vui lòng thử lại sau ít phút"


def _write_restart_log(
    db: Session,
    user: User,
    env_label: str,
    project: str,
    application: str,
    result: argocd_service.WorkloadRestartResult,
) -> None:
    log = RestartLog(
        env=env_label,
        project=project,
        application=application,
        workload_kind=result.workload_kind,
        workload_name=result.workload_name,
        user_id=user.id,
        user_email=user.email,
        ok=result.ok,
        message=result.message,
    )
    db.add(log)
    try:
        db.commit()
    except Exception:
        # Ghi log that bai (vd DB tam thoi mat ket noi) KHONG duoc lam sai lech ket qua
        # da tra ve cho nguoi dung (ArgoCD co the da restart THAT SU thanh cong roi) - chi
        # log loi, rollback session de khong ket dinh transaction, KHONG raise tiep (AC8:
        # khong crash request).
        logger.exception("Ghi RestartLog thất bại cho %s/%s (%s)", project, application, env_label)
        db.rollback()


def record_blocked_attempt(
    db: Session, user: User, env_label: str, project: str, application: str, reason: str
) -> None:
    """Ghi 1 dong RestartLog (ok=False) cho cac lan bam nut Restart bi chan NGAY TAI
    ROUTER, TRUOC KHI restart_app() duoc goi (CSRF sai/het han, thieu quyen can_access_*) -
    2 nhanh nay von KHONG di qua restart_app nen se khong de lai dau vet gi neu khong goi
    ham nay, lam mat kha nang phat hien do quet khai thac lo hong whitelist (#1). Dung
    chung logic ghi/commit voi _write_restart_log de nhat quan hanh vi loi DB."""
    logger.warning(
        "Restart bị chặn trước khi gọi ArgoCD cho %s/%s (env=%s) bởi user '%s': %s",
        project,
        application,
        env_label,
        user.email,
        reason,
    )
    result = argocd_service.WorkloadRestartResult(ok=False, message=reason)
    _write_restart_log(db, user, env_label, project, application, result)


def restart_app(db: Session, user: User, env_label: str, project: str, application: str) -> argocd_service.WorkloadRestartResult:
    """env_label: "staging" hoac "production" - dung de ghi log (RestartLog.env) VA de doi
    chieu whitelist EnvAppCache (env=env_label, project, application) TRUOC khi cho phep
    goi ArgoCD (lo hong #1: khac /toggle, o day project/application duoc PATCH thang vao
    ArgoCD nen bat buoc phai la app THAT SU duoc hien thi cho user tren trang tuong ung,
    KHONG chi dua vao co TOAN CUC can_access_staging/can_access_production da kiem o
    router). Cac lan bi chan (regex/whitelist/rate-limit) van duoc ghi 1 dong RestartLog
    (ok=False) giong het duong loi khac, KHONG can code rieng o router."""
    if not _APP_NAME_RE.fullmatch(project) or not _APP_NAME_RE.fullmatch(application):
        result = argocd_service.WorkloadRestartResult(ok=False, message=_INVALID_NAME_MESSAGE)
        _write_restart_log(db, user, env_label, project, application, result)
        return result

    whitelisted = (
        db.query(EnvAppCache).filter_by(env=env_label, project=project, application=application).first()
    )
    if whitelisted is None:
        logger.warning(
            "Restart bị từ chối: %s/%s (env=%s) không có trong EnvAppCache, user '%s'",
            project,
            application,
            env_label,
            user.email,
        )
        result = argocd_service.WorkloadRestartResult(ok=False, message=_NOT_WHITELISTED_MESSAGE)
        _write_restart_log(db, user, env_label, project, application, result)
        return result

    cooldown_seconds = get_app_setting(db).restart_cooldown_seconds
    if not _check_restart_rate_limit(env_label, project, application, cooldown_seconds):
        logger.warning(
            "Rate limit chặn Restart %s/%s (env=%s) cho user '%s' - thao tác quá nhanh",
            project,
            application,
            env_label,
            user.email,
        )
        result = argocd_service.WorkloadRestartResult(ok=False, message=_restart_rate_limit_message(cooldown_seconds))
        _write_restart_log(db, user, env_label, project, application, result)
        return result

    result = argocd_service.restart_workload(db, env_label, project, application)
    _write_restart_log(db, user, env_label, project, application, result)
    return result


@dataclass
class BulkRestartResult:
    ok: bool
    message: str


def bulk_restart_apps(
    db: Session, user: User, env_label: str, apps: list[tuple[str, str]]
) -> BulkRestartResult:
    """Restart NHIEU app CHON CUNG LUC (nut "Restart" trong toolbar bulk cua /staging,
    /production khi tick nhieu checkbox) - KHAC bulk_toggle (env_toggle_service.py, 1 GIT
    COMMIT duy nhat cho ca batch): ArgoCD KHONG co API restart nhieu app tuy y trong 1 lan
    goi, nen o day chi don gian LAP goi lai restart_app() (dung 1 cho, KHONG lap logic
    regex/whitelist/rate-limit/ghi RestartLog rieng) cho TUNG app mot - moi app hoan toan
    DOC LAP (app nay that bai/dinh cooldown rieng KHONG chan cac app con lai trong batch).

    Tra ve ok=True CHI KHI TAT CA app deu restart thanh cong - 1 app that bai van coi ca
    batch la ok=False de nguoi dung de y kiem tra (message liet ke ro app nao that bai va
    ly do), nhung TAT CA app van duoc thu (khong dung som khi gap 1 loi)."""
    apps = list(dict.fromkeys(apps))
    if not apps:
        return BulkRestartResult(ok=False, message="Chưa chọn ứng dụng nào")

    outcomes = [
        (project, application, restart_app(db, user, env_label, project, application))
        for project, application in apps
    ]
    ok_count = sum(1 for _, _, result in outcomes if result.ok)
    fail_count = len(outcomes) - ok_count

    if fail_count == 0:
        return BulkRestartResult(ok=True, message=f"Đã khởi động lại {ok_count} ứng dụng")

    failed_summary = "; ".join(
        f"{project}/{application}: {result.message}"
        for project, application, result in outcomes
        if not result.ok
    )
    return BulkRestartResult(
        ok=False,
        message=f"Khởi động lại thành công {ok_count}/{len(outcomes)} ứng dụng. Thất bại - {failed_summary}",
    )
