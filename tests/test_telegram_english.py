"""Tin Telegram cua Replicas staging/production va Schedule phai bang tieng Anh (UI van
giu tieng Viet)."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.models import AppSchedule, EnvAppCache, Role
from app.services import (
    argocd_service,
    env_toggle_service,
    git_service,
    notify_service,
    production_service,
    schedule_service,
    staging_service,
)
from app.services.telegram_text import to_english

_VIETNAMESE = "ăâđêôơưáàảãạấầẩẫậắằẳẵặéèẻẽẹếềểễệíìỉĩịóòỏõọốồổỗộớờởỡợúùủũụứừửữựýỳỷỹỵ"


@pytest.fixture()
def sent(monkeypatch):
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(notify_service, "send_infra_toggle_telegram", lambda text, env: messages.append((env, text)))
    return messages


def _assert_english(text: str) -> None:
    assert not any(ch in _VIETNAMESE for ch in text.lower()), text


def _ok(changed, message="Đã cập nhật", pending=False):
    return git_service.ReplicasToggleResult(ok=True, message=message, changed=changed, pending_merge_request=pending)


def test_toggle_bulk_and_turn_off_all_messages_are_english(make_user_factory, db_session, sent, monkeypatch):
    user = make_user_factory(role=Role.SUPER_ADMIN, email="sa@example.com")
    monkeypatch.setattr(staging_service, "_push_config", lambda db: None)
    monkeypatch.setattr(production_service, "_push_config", lambda db: None)
    monkeypatch.setattr(git_service, "toggle_staging_app", lambda p, a, r, e, c: _ok(((p, a),)))
    monkeypatch.setattr(
        git_service, "toggle_production_app",
        lambda p, a, r, e, c: _ok(((p, a),), "Đã tạo Merge Request vào nhánh master", pending=True),
    )
    monkeypatch.setattr(git_service, "bulk_toggle_staging_apps", lambda apps, en, e, c: _ok(tuple(apps)))
    monkeypatch.setattr(git_service, "turn_off_all_staging", lambda e, c: _ok((("grp", "svc"),), "Đã tắt tất cả staging"))

    staging_service.toggle_app(db_session, user, "grp", "svc", True, 2)
    production_service.toggle_app(db_session, user, "grp", "svc", False, None)
    staging_service.bulk_toggle(db_session, user, [("grp", "a"), ("grp", "b")], False)
    staging_service.turn_off_all(db_session, user)

    texts = [text for _, text in sent]
    assert "<b>Turn on staging: grp/svc (replicas=2)" in texts[0] and "[Scheduled]" not in "".join(texts)
    assert "Turn on staging: grp/svc (replicas=2)" in texts[0] and "- Performed by:</b> sa@example.com" in texts[0]
    assert "- Result:</b> Updated" in texts[0]
    assert "Turn off production: grp/svc (replicas=0)" in texts[1] and "Merge Request created to branch master" in texts[1]
    assert "Bulk turn off staging (2 applications)" in texts[2]
    assert "<b>- Applications:</b>\n  • grp/a\n  • grp/b" in texts[2]  # nhieu app: moi app 1 dong
    assert "Turn off all staging" in texts[3] and "All staging apps turned off" in texts[3]
    for text in texts:
        _assert_english(text)


def test_scheduled_restart_and_toggle_messages_are_english(make_user_factory, db_session, sent, monkeypatch):
    user = make_user_factory(role=Role.SUPER_ADMIN, email="sa@example.com")
    db_session.add(EnvAppCache(env="production", project="grp", application="svc", replicas="1"))
    now = datetime(2026, 10, 16, 3, 0)
    for action in ("restart", "stop"):
        db_session.add(AppSchedule(action=action, env="production", project="grp", application="svc", frequency="once",
                                   run_once_at=now, next_run_at=now, is_active=True,
                                   created_by_id=user.id, created_by_email=user.email))
    db_session.commit()
    monkeypatch.setattr(schedule_service.restart_service, "_restart_last_call", {})
    monkeypatch.setattr(
        argocd_service, "restart_workload",
        lambda db, env, p, a: argocd_service.WorkloadRestartResult(ok=True, message="Đã khởi động lại Deployment/svc"),
    )
    monkeypatch.setattr(production_service, "_push_config", lambda db: None)
    monkeypatch.setattr(git_service, "toggle_production_app", lambda p, a, r, e, c: _ok(((p, a),)))

    schedule_service.run_due_schedules(db_session, now=now)

    texts = [text for _, text in sent]
    assert any("[Scheduled] Restart production: grp/svc" in t and "Restarted Deployment/svc" in t and "- Scheduled by:</b> sa@example.com" in t for t in texts)
    assert any("[Scheduled] Turn off production: grp/svc (replicas=0)" in t for t in texts)
    for text in texts:
        _assert_english(text)


@pytest.mark.parametrize(
    "vi, en",
    [
        ("File charts/values-staging.yaml không tồn tại", "File charts/values-staging.yaml does not exist"),
        ("File x.yaml không thay đổi (giá trị hiện tại đã đúng)", "File x.yaml unchanged (value already up to date)"),
        ("Repo URL (Setting > General) chưa được cấu hình", "Repo URL (Setting > General) is not configured"),
        ("Vui lòng đợi 60 giây rồi thử lại", "Restart cooldown: please wait 60 seconds and try again"),
        ("Người tạo lịch (a@b.c) không còn quyền thực hiện", "Schedule creator (a@b.c) no longer has permission"),
        ("git push failed: rejected", "git push failed: rejected"),
    ],
)
def test_to_english(vi, en):
    assert to_english(vi) == en


def test_manual_bulk_many_apps_split_under_limit(make_user_factory, db_session, sent, monkeypatch):
    user = make_user_factory(role=Role.SUPER_ADMIN, email="sa@example.com")
    monkeypatch.setattr(staging_service, "_push_config", lambda db: None)
    monkeypatch.setattr(git_service, "bulk_toggle_staging_apps", lambda apps, en, e, c: _ok(tuple(apps)))
    apps = [("accountsystem", f"very-long-application-name-{i:03d}") for i in range(250)]
    staging_service.bulk_toggle(db_session, user, apps, False)

    texts = [text for _, text in sent]
    assert len(texts) > 1 and all(len(t) < 4096 for t in texts)
    assert f"(1/{len(texts)})" in texts[0]
    assert sum(t.count("  • accountsystem/very-long-application-name-") for t in texts) == 250
