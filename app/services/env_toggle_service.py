"""Logic dung CHUNG cho tinh nang Bat/Tat staging VA Production replicas - 2 tinh nang
gan nhu giong het nhau (chi khac file dich + nhanh git; production khong co "Tat tat
ca") nen gop business logic vao day. staging_service.py/production_service.py chi la
wrapper mong, truyen vao cac ham git_service tuong ung cho tung moi truong.

Danh sach app hien thi tren UI luon doc tu bang cache `EnvAppCache` (nhanh, khong goi
git luc load trang) - GIONG HET co che catalog Group/Project
(catalog_service.sync_catalog_from_chart_repo): dong bo dinh ky qua scheduler + nut "Lam
moi" thu cong (sync_apps() ben duoi), KHONG con fetch git moi lan xem trang nhu ban dau.
Sau 1 lan toggle thanh cong, cache duoc ghi de truc tiep (write-through) de UI phan anh
ngay, khong can doi den lan sync dinh ky tiep theo - TRU khi ket qua la
pending_merge_request=True (Production, AppSetting.production_use_merge_request=True):
luc do thay doi moi chi nam trong 1 Merge Request cho duyet, CHUA thuc su ap dung len
nhanh master, nen KHONG ghi de cache (se sai neu ghi) - cho toi khi sync_apps() dinh ky
(hoac thu cong) doc lai gia tri that su tu git sau khi MR duoc merge.

KHONG kiem tra quyen o day (giong task_service.py) - phan quyen thuoc ve router."""

import html
import logging
from datetime import datetime
from dataclasses import dataclass
from typing import Callable

from sqlalchemy.orm import Session

from app.models import AppReplicasRestore, EnvAppCache, User
from app.services import git_service, notify_service
from app.services.telegram_text import to_english


logger = logging.getLogger(__name__)


@dataclass
class EnvActionResult:
    ok: bool
    message: str
    skipped: bool = False
    """Chi dung khi scheduled=True: git bao file khong thay doi (gia tri da dung) -> lich
    coi la BO QUA (khong phai loi), khong gui Telegram."""
    busy: bool = False
    """Chi dung khi scheduled=True: git lock dang bi thao tac khac giu (GitLockBusyError) ->
    lich se thu lai o phut sau, KHONG gui Telegram cho lan nay."""


def _normalize_env(env_label: str) -> str:
    """Chuan hoa env_label ve "staging"/"production" - ENV_LABEL cua staging_service.py va
    production_service.py da dung dung 2 gia tri nay, nhung ham nay van map thu (chua
    "production", khong phan biet hoa/thuong -> "production", con lai -> "staging") de an
    toan neu tuong lai co nhan hien thi khac dua vao."""
    return "production" if "production" in env_label.lower() else "staging"


def commit_line(commit_url: str) -> str:
    """Dong "- Commit: <link>" trong tin Telegram (text = 8 ky tu dau cua SHA)."""
    sha = commit_url.rsplit("/", 1)[-1][:8]
    return f'<b>- Commit:</b> <a href="{html.escape(commit_url, quote=True)}">{html.escape(sha)}</a>'


def git_link_line(result: git_service.ReplicasToggleResult) -> str | None:
    """Che do Merge Request: dong "- Merge Request: !<so>" (link MR); con lai dong Commit.
    None neu khong co link nao."""
    if result.merge_request_url:
        number = result.merge_request_url.rsplit("/", 1)[-1]
        text = f"!{number}" if number.isdigit() else "Open"
        return f'<b>- Merge Request:</b> <a href="{html.escape(result.merge_request_url, quote=True)}">{html.escape(text)}</a>'
    if result.commit_url:
        return commit_line(result.commit_url)
    return None


# Telegram gioi han 4096 ky tu/tin - de du cho the HTML/so trang "(1/3)".
TELEGRAM_CHUNK_LIMIT = 3500


def send_chunked_telegram(env: str, header: list[str], items: list[str], footer: list[str]) -> None:
    """Gui 1 tin Telegram, TACH thanh nhieu tin (danh so "(i/n)" o dong dau) neu danh sach
    `items` qua dai - moi item 1 dong nen khong bao gio cat ngang ten app/the HTML. Header +
    footer lap lai o moi tin. Loi gui chi log (khong anh huong ket qua lich)."""
    fixed = len("\n".join(header + footer)) + 20
    chunks: list[list[str]] = [[]]
    for line in items:
        if chunks[-1] and fixed + len("\n".join(chunks[-1] + [line])) > TELEGRAM_CHUNK_LIMIT:
            chunks.append([])
        chunks[-1].append(line)
    for i, chunk in enumerate(chunks, 1):
        lines = list(header)
        if len(chunks) > 1:
            lines[0] = f"{lines[0]} ({i}/{len(chunks)})"
        try:
            notify_service.send_infra_toggle_telegram("\n".join(lines + chunk + footer), env)
        except Exception:
            logger.exception("Gửi Telegram (%s) thất bại", env)


def notify(action_label: str, actor_email: str, result: git_service.ReplicasToggleResult, env_label: str) -> None:
    """1 app: "- Applications: p/a" cung dong; nhieu app: moi app 1 dong, tach nhieu tin
    neu danh sach dai (send_chunked_telegram)."""
    icon = "✅" if result.ok else "⚠️"
    header = [
        f"{icon} <b>{action_label}</b>",
        f"<b>- Performed by:</b> {html.escape(actor_email)}",
        f"<b>- Result:</b> {html.escape(to_english(result.message))}",
    ]
    link = git_link_line(result)
    if link:
        header.append(link)
    items: list[str] = []
    if len(result.changed) == 1:
        project, application = result.changed[0]
        header.append(f"<b>- Applications:</b> {html.escape(f'{project}/{application}')}")
    elif result.changed:
        header.append("<b>- Applications:</b>")
        items = [f"  • {html.escape(f'{p}/{a}')}" for p, a in result.changed]
    send_chunked_telegram(_normalize_env(env_label), header, items, [])


def sync_apps(
    db: Session,
    env_label: str,
    list_fn: Callable[[git_service.EnvReplicasPushConfig], list[tuple[str, str, str]]],
    push_config: git_service.EnvReplicasPushConfig,
) -> dict:
    """Dong bo cache EnvAppCache tu git (list_fn quet thu muc charts/{group}/{application}/
    tren dung nhanh + doc replicas tu file values) - GIONG HET
    catalog_service.sync_catalog_from_chart_repo: tao moi/cap nhat dong khop, xoa dong
    khong con xuat hien trong git nua. Co the raise git_service.GitError - de caller tu
    quyet dinh xu ly (job dinh ky log+bo qua, route thu cong tra loi ro cho nguoi dung)."""
    entries = list_fn(push_config)

    seen: set[tuple[str, str]] = set()
    created = updated = 0
    for project, application, replicas in entries:
        seen.add((project, application))
        row = db.query(EnvAppCache).filter_by(env=env_label, project=project, application=application).first()
        if row is None:
            db.add(EnvAppCache(env=env_label, project=project, application=application, replicas=replicas))
            created += 1
        elif row.replicas != replicas:
            row.replicas = replicas
            row.synced_at = datetime.utcnow()
            updated += 1

    deleted = 0
    for row in db.query(EnvAppCache).filter_by(env=env_label).all():
        if (row.project, row.application) not in seen:
            db.delete(row)
            deleted += 1

    db.commit()
    return {"created": created, "updated": updated, "deleted": deleted, "total": len(entries)}


def write_through_cache(db: Session, env_label: str, pairs: tuple[tuple[str, str], ...], replicas: int) -> None:
    """Ghi de truc tiep replicas cho cac dong da toggle thanh cong - tranh phai doi den
    lan sync dinh ky tiep theo UI moi cap nhat."""
    now = datetime.utcnow()
    for project, application in pairs:
        row = db.query(EnvAppCache).filter_by(env=env_label, project=project, application=application).first()
        if row is not None:
            row.replicas = str(replicas)
            row.synced_at = now
    db.commit()


def _cached_replicas(db: Session, env_label: str, project: str, application: str) -> int:
    row = db.query(EnvAppCache).filter_by(env=env_label, project=project, application=application).first()
    return int(row.replicas) if row is not None and str(row.replicas).isdigit() else 0


def remembered_replicas(db: Session, env_label: str, project: str, application: str) -> int | None:
    """So pod da nho truoc lan tat gan nhat (None neu khong co) - dung khi bat lai."""
    row = db.query(AppReplicasRestore).filter_by(env=env_label, project=project, application=application).first()
    return row.replicas if row is not None and row.replicas > 0 else None


def remember_replicas(db: Session, env_label: str, pairs) -> None:
    """Nho so pod HIEN TAI (doc EnvAppCache) cua cac app sap bi tat - PHAI goi TRUOC
    write_through_cache (luc do cache van con so pod cu). App dang tat san (0) thi giu
    nguyen gia tri da nho truoc do."""
    for project, application in pairs:
        current = _cached_replicas(db, env_label, project, application)
        if current <= 0:
            continue
        row = db.query(AppReplicasRestore).filter_by(env=env_label, project=project, application=application).first()
        if row is None:
            db.add(AppReplicasRestore(env=env_label, project=project, application=application, replicas=current))
        else:
            row.replicas = current
    db.commit()


def forget_replicas(db: Session, env_label: str, pairs) -> None:
    """Xoa so pod da nho sau khi bat lai thanh cong."""
    for project, application in pairs:
        db.query(AppReplicasRestore).filter_by(env=env_label, project=project, application=application).delete()
    db.commit()


def _track_replicas(db: Session, env_label: str, enabled: bool, result: git_service.ReplicasToggleResult) -> None:
    if result.ok and result.changed:
        if enabled:
            forget_replicas(db, env_label, result.changed)
        else:
            remember_replicas(db, env_label, result.changed)


def toggle_app(
    db: Session,
    user: User,
    project: str,
    application: str,
    enabled: bool,
    replicas: int | None,
    env_label: str,
    toggle_fn: Callable[[str, str, int, str, git_service.EnvReplicasPushConfig], git_service.ReplicasToggleResult],
    push_config: git_service.EnvReplicasPushConfig,
    scheduled: bool = False,
) -> EnvActionResult:
    """scheduled=True khi goi tu job chay lich (menu Schedule) - chi them nhan "[Scheduled]"
    vao tin Telegram, khong doi hanh vi. Tat -> replicas=0; Bat -> dung `replicas` neu > 0, mac dinh 1 neu khong nhap/nhap
    <= 0 (o nhap replicas chi co hieu luc khi da bat cong tac, dung yeu cau UI). Khong
    nhap so pod khi bat -> dung so pod da nho truoc lan tat gan nhat (AppReplicasRestore),
    khong co thi 1. Tat thanh cong -> nho so pod hien tai; bat thanh cong -> xoa so da nho.
    Khong tu validate project/application truoc - toggle_fn (git_service) da tu bao loi ro
    rang neu cap nay khong ton tai san trong file tren git."""
    if enabled:
        effective_replicas = (
            replicas if replicas and replicas > 0 else (remembered_replicas(db, env_label, project, application) or 1)
        )
    else:
        effective_replicas = 0

    try:
        result = toggle_fn(project, application, effective_replicas, user.email, push_config)
    except git_service.GitError as exc:
        if scheduled and isinstance(exc, git_service.GitLockBusyError):
            return EnvActionResult(ok=False, message=str(exc), busy=True)
        result = git_service.ReplicasToggleResult(ok=False, message=str(exc))

    # ok=False + changed khac rong = git bao "file khong thay doi" (gia tri da dung san).
    # Bam tay van bao loi nhu cu; chay theo lich thi coi la bo qua, khong gui Telegram.
    if scheduled and not result.ok and result.changed:
        return EnvActionResult(ok=True, message=result.message, skipped=True)

    _track_replicas(db, env_label, enabled, result)
    if result.ok and result.changed and not result.pending_merge_request:
        write_through_cache(db, env_label, result.changed, effective_replicas)

    verb = "Turn on" if enabled else "Turn off"
    action_label = f"{verb} {env_label}: {project}/{application} (replicas={effective_replicas})"
    if scheduled:
        action_label = f"[Scheduled] {action_label}"
    notify(action_label, user.email, result, env_label)
    return EnvActionResult(ok=result.ok, message=result.message)


def bulk_toggle(
    db: Session,
    user: User,
    apps: list[tuple[str, str]],
    enabled: bool,
    env_label: str,
    bulk_toggle_fn: Callable[
        [list[tuple[str, str]], bool, str, git_service.EnvReplicasPushConfig], git_service.ReplicasToggleResult
    ],
    push_config: git_service.EnvReplicasPushConfig,
    set_fn: Callable[[list[tuple[str, str, int]], str, git_service.EnvReplicasPushConfig], git_service.ReplicasToggleResult]
    | None = None,
) -> EnvActionResult:
    """Bat/Tat nhieu app CHON CUNG LUC - Tat: replicas=0 (nho so pod cu cua tung app). Bat:
    moi app ve DUNG so pod da nho truoc lan tat (khong co thi 1) qua set_fn (1 commit, so
    pod rieng tung app); khong truyen set_fn thi nhu cu (bulk_toggle_fn, tat ca = 1). Cap
    nao khong thuc su ton tai trong file se tu bi bo qua boi git_service."""
    apps = list(dict.fromkeys(apps))
    if not apps:
        return EnvActionResult(ok=False, message="Chưa chọn ứng dụng nào")

    targets = {
        (project, application): (remembered_replicas(db, env_label, project, application) or 1) if enabled else 0
        for project, application in apps
    }
    # Chi dung set_fn (so pod rieng tung app) khi THUC SU co app can khoi phuc != 1 - con lai
    # di dung duong cu bulk_toggle_fn (tat ca = 1/0), khong doi hanh vi khi khong co gi da nho.
    use_set_fn = enabled and set_fn is not None and any(n != 1 for n in targets.values())
    try:
        if use_set_fn:
            entries = [(project, application, n) for (project, application), n in targets.items()]
            result = set_fn(entries, user.email, push_config)
        else:
            result = bulk_toggle_fn(apps, enabled, user.email, push_config)
    except git_service.GitError as exc:
        result = git_service.ReplicasToggleResult(ok=False, message=str(exc))

    _track_replicas(db, env_label, enabled, result)
    if result.ok and result.changed and not result.pending_merge_request:
        # So pod THUC SU da ghi len git: set_fn -> theo targets; bulk_toggle_fn -> 1/0 chung.
        applied = targets if (use_set_fn or not enabled) else {pair: 1 for pair in targets}
        for pair in result.changed:
            write_through_cache(db, env_label, (pair,), applied.get(tuple(pair), 1 if enabled else 0))

    verb = "Turn on" if enabled else "Turn off"
    action_label = f"Bulk {verb.lower()} {env_label} ({len(apps)} applications)"
    notify(action_label, user.email, result, env_label)
    return EnvActionResult(ok=result.ok, message=result.message)


def list_apps_with_replicas(db: Session, env_label: str) -> list[dict]:
    """Doc TU CACHE DB (khong goi git, nhanh) - giong cach task_form.html doc catalog
    Group/Project da sync san, thay vi fetch git moi lan xem trang."""
    rows = (
        db.query(EnvAppCache)
        .filter_by(env=env_label)
        .order_by(EnvAppCache.project, EnvAppCache.application)
        .all()
    )
    # So pod da nho truoc lan tat gan nhat -> UI dien san vao o replicas khi bat lai.
    restore = {
        (r.project, r.application): r.replicas
        for r in db.query(AppReplicasRestore).filter_by(env=env_label).all()
        if r.replicas > 0
    }
    return [
        {
            "project": row.project,
            "application": row.application,
            "replicas": row.replicas,
            "enabled": row.replicas != "0",
            "restore_replicas": restore.get((row.project, row.application)),
        }
        for row in rows
    ]
