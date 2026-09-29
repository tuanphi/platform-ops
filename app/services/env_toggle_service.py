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
from datetime import datetime
from dataclasses import dataclass
from typing import Callable

from sqlalchemy.orm import Session

from app.models import EnvAppCache, User
from app.services import git_service, notify_service


@dataclass
class EnvActionResult:
    ok: bool
    message: str


def _normalize_env(env_label: str) -> str:
    """Chuan hoa env_label ve "staging"/"production" - ENV_LABEL cua staging_service.py va
    production_service.py da dung dung 2 gia tri nay, nhung ham nay van map thu (chua
    "production", khong phan biet hoa/thuong -> "production", con lai -> "staging") de an
    toan neu tuong lai co nhan hien thi khac dua vao."""
    return "production" if "production" in env_label.lower() else "staging"


def notify(action_label: str, actor_email: str, result: git_service.ReplicasToggleResult, env_label: str) -> None:
    icon = "✅" if result.ok else "⚠️"
    lines = [f"{icon} <b>{action_label}</b>", f"<b>- Người thực hiện:</b> {html.escape(actor_email)}"]
    if result.changed:
        apps_text = ", ".join(f"{p}/{a}" for p, a in result.changed)
        lines.append(f"<b>- Ứng dụng:</b> {html.escape(apps_text)}")
    lines.append(f"<b>- Kết quả:</b> {html.escape(result.message)}")
    notify_service.send_infra_toggle_telegram("\n".join(lines), _normalize_env(env_label))


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
) -> EnvActionResult:
    """Tat -> replicas=0; Bat -> dung `replicas` neu > 0, mac dinh 1 neu khong nhap/nhap
    <= 0 (o nhap replicas chi co hieu luc khi da bat cong tac, dung yeu cau UI). Khong tu
    validate project/application truoc - toggle_fn (git_service) da tu bao loi ro rang
    neu cap nay khong ton tai san trong file tren git."""
    effective_replicas = (replicas if replicas and replicas > 0 else 1) if enabled else 0

    try:
        result = toggle_fn(project, application, effective_replicas, user.email, push_config)
    except git_service.GitError as exc:
        result = git_service.ReplicasToggleResult(ok=False, message=str(exc))

    if result.ok and result.changed and not result.pending_merge_request:
        write_through_cache(db, env_label, result.changed, effective_replicas)

    verb = "Bật" if enabled else "Tắt"
    action_label = f"{verb} {env_label}: {project}/{application} (replicas={effective_replicas})"
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
) -> EnvActionResult:
    """Bat/Tat nhieu app CHON CUNG LUC - replicas luon la 1 (bat) hoac 0 (tat), khong nhan
    so tuy chinh (muon > 1 phai sua tung app rieng qua toggle_app). Cap nao khong thuc su
    ton tai trong file se tu bi bo qua boi bulk_toggle_fn (git_service)."""
    apps = list(dict.fromkeys(apps))
    if not apps:
        return EnvActionResult(ok=False, message="Chưa chọn ứng dụng nào")

    replicas = 1 if enabled else 0
    try:
        result = bulk_toggle_fn(apps, enabled, user.email, push_config)
    except git_service.GitError as exc:
        result = git_service.ReplicasToggleResult(ok=False, message=str(exc))

    if result.ok and result.changed and not result.pending_merge_request:
        write_through_cache(db, env_label, result.changed, replicas)

    verb = "Bật" if enabled else "Tắt"
    action_label = f"{verb} {env_label} hàng loạt ({len(apps)} ứng dụng)"
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
    return [
        {
            "project": row.project,
            "application": row.application,
            "replicas": row.replicas,
            "enabled": row.replicas != "0",
        }
        for row in rows
    ]
