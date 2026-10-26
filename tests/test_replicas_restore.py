"""Tat app (1 app / hang loat / Tat tat ca) o /staging, /production nho so pod cu; bat lai
khoi phuc dung so pod do (thay vi luon 1)."""

import re

from app.models import AppReplicasRestore, EnvAppCache, Role
from app.services import git_service
from tests.conftest import make_user


def _cache(db_session, env, app, replicas):
    db_session.add(EnvAppCache(env=env, project="core", application=app, replicas=replicas))
    db_session.commit()


def _replicas(db_session, env, app):
    return db_session.query(EnvAppCache).filter_by(env=env, application=app).one().replicas


def _ok(changed):
    return git_service.ReplicasToggleResult(ok=True, message="Đã cập nhật", changed=tuple(changed))


def _setup(monkeypatch, calls):
    monkeypatch.setattr("app.services.env_toggle_service.notify_service.send_infra_toggle_telegram", lambda msg, env: None)
    for env in ("staging", "production"):
        monkeypatch.setattr(
            git_service, f"toggle_{env}_app",
            lambda p, a, r, e, c, env=env: calls.append((env, "one", a, r)) or _ok([(p, a)]),
        )
        monkeypatch.setattr(
            git_service, f"bulk_toggle_{env}_apps",
            lambda apps, en, e, c, env=env: calls.append((env, "bulk", tuple(a for _, a in apps), 1 if en else 0)) or _ok(apps),
        )
        monkeypatch.setattr(
            git_service, f"set_{env}_replicas",
            lambda entries, e, c, env=env: calls.append((env, "set", tuple((a, n) for _, a, n in entries))) or _ok([(p, a) for p, a, _ in entries]),
        )
    monkeypatch.setattr(
        git_service, "turn_off_all_staging",
        lambda e, c: calls.append(("staging", "all")) or _ok([("core", "api"), ("core", "worker")]),
    )


def test_single_toggle_off_then_on_restores_and_prefills(client_factory, db_session, monkeypatch):
    _cache(db_session, "staging", "api", "3")
    calls = []
    _setup(monkeypatch, calls)
    client = client_factory(user=make_user(db_session, email="sa@example.com", role=Role.SUPER_ADMIN))
    json = {"Accept": "application/json"}

    client.post("/staging/toggle", data={"project": "core", "application": "api", "enabled": "0"}, headers=json)
    assert _replicas(db_session, "staging", "api") == "0"
    assert db_session.query(AppReplicasRestore).one().replicas == 3

    # O replicas cua app dang tat dien san so pod da nho (3) thay vi 1.
    page = client.get("/staging").text
    assert re.search(r'name="replicas" min="1" value="3"', page)

    # Bat khong gui so pod -> dung so da nho; bat xong xoa so da nho.
    client.post("/staging/toggle", data={"project": "core", "application": "api", "enabled": "1"}, headers=json)
    assert calls[-1] == ("staging", "one", "api", 3)
    assert _replicas(db_session, "staging", "api") == "3"
    assert db_session.query(AppReplicasRestore).count() == 0


def test_bulk_off_then_on_restores_each_app(client_factory, db_session, monkeypatch):
    _cache(db_session, "production", "api", "3")
    _cache(db_session, "production", "worker", "2")
    _cache(db_session, "production", "cron", "1")
    calls = []
    _setup(monkeypatch, calls)
    client = client_factory(user=make_user(db_session, email="sa@example.com", role=Role.SUPER_ADMIN))
    apps = ["core|api", "core|worker", "core|cron"]

    client.post("/production/bulk-toggle", data={"apps": apps, "enabled": "0"}, headers={"Accept": "application/json"})
    remembered = {r.application: r.replicas for r in db_session.query(AppReplicasRestore).all()}
    assert remembered == {"api": 3, "worker": 2, "cron": 1}

    client.post("/production/bulk-toggle", data={"apps": apps, "enabled": "1"}, headers={"Accept": "application/json"})
    assert calls[-1] == ("production", "set", (("api", 3), ("worker", 2), ("cron", 1)))
    assert [_replicas(db_session, "production", a) for a in ("api", "worker", "cron")] == ["3", "2", "1"]
    assert db_session.query(AppReplicasRestore).count() == 0


def test_turn_off_all_remembers_replicas(client_factory, db_session, monkeypatch):
    _cache(db_session, "staging", "api", "4")
    _cache(db_session, "staging", "worker", "0")
    calls = []
    _setup(monkeypatch, calls)
    client = client_factory(user=make_user(db_session, email="sa@example.com", role=Role.SUPER_ADMIN))

    client.post("/staging/turn-off-all", headers={"Accept": "application/json"})
    assert {r.application: r.replicas for r in db_session.query(AppReplicasRestore).all()} == {"api": 4}


def test_bulk_on_without_memory_keeps_old_path(client_factory, db_session, monkeypatch):
    _cache(db_session, "staging", "api", "0")
    calls = []
    _setup(monkeypatch, calls)
    client = client_factory(user=make_user(db_session, email="sa@example.com", role=Role.SUPER_ADMIN))

    client.post("/staging/bulk-toggle", data={"apps": ["core|api"], "enabled": "1"}, headers={"Accept": "application/json"})
    assert calls == [("staging", "bulk", ("api",), 1)]
