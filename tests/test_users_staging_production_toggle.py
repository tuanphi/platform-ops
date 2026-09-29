"""Kiểm thử luồng chính app/routers/users_router.py::toggle_staging_permission /
toggle_production_permission (quyền sửa Replicas), toggle_staging_view_permission /
toggle_production_view_permission (quyền CHỈ XEM, read-only), và
toggle_staging_restart_permission / toggle_production_restart_permission (quyền Restart,
tách biệt hoàn toàn với 2 nhóm trên) - 6 công tắc TỔNG (Settings > Users) độc lập nhau,
chỉ Super Admin (require_settings_access) được đổi."""

from app.models import Role
from tests.conftest import make_user


def test_toggle_staging_permission_flips_flag(client_factory, db_session):
    sa = make_user(db_session, email="sa@example.com", role=Role.SUPER_ADMIN)
    target = make_user(db_session, email="target@example.com", role=Role.USER, can_toggle_staging=False)
    client = client_factory(user=sa)

    resp = client.post(f"/users/{target.id}/staging-toggle", headers={"Accept": "application/json"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["can_toggle_staging"] is True
    db_session.refresh(target)
    assert target.can_toggle_staging is True


def test_toggle_production_permission_independent_of_staging(client_factory, db_session):
    """Bat can_toggle_production KHONG duoc lam doi can_toggle_staging (2 cot doc lap)."""
    sa = make_user(db_session, email="sa2@example.com", role=Role.SUPER_ADMIN)
    target = make_user(
        db_session, email="target2@example.com", role=Role.USER, can_toggle_staging=True, can_toggle_production=False
    )
    client = client_factory(user=sa)

    resp = client.post(f"/users/{target.id}/production-toggle", headers={"Accept": "application/json"})

    assert resp.status_code == 200
    assert resp.json()["can_toggle_production"] is True
    db_session.refresh(target)
    assert target.can_toggle_production is True
    assert target.can_toggle_staging is True  # khong bi anh huong


def test_toggle_staging_permission_denied_for_non_super_admin(client_factory, db_session):
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    target = make_user(db_session, email="target3@example.com", role=Role.USER)
    client = client_factory(user=admin)

    resp = client.post(f"/users/{target.id}/staging-toggle", headers={"Accept": "application/json"})

    assert resp.status_code == 403


def test_set_user_role_ajax_response_includes_role_and_permission_flags(client_factory, db_session):
    """Regression: doi role (vd len Super Admin) anh huong TRUC TIEP giao dien cot
    Replicas Staging/Production trong users.html (JS ve lai 2 o do dua tren response
    nay, khong reload trang) - response AJAX phai tra du role + 2 co quyen, khong chi
    message chung chung nhu truoc."""
    sa = make_user(db_session, email="sa4@example.com", role=Role.SUPER_ADMIN)
    target = make_user(
        db_session, email="target4@example.com", role=Role.USER, can_toggle_staging=True, can_toggle_production=False
    )
    client = client_factory(user=sa)

    resp = client.post(
        f"/users/{target.id}/role", data={"role": "super_admin"}, headers={"Accept": "application/json"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["role"] == "super_admin"
    assert body["can_toggle_staging"] is True
    assert body["can_toggle_production"] is False
    assert body["can_view_staging"] is False
    assert body["can_view_production"] is False
    assert body["can_toggle_restart_staging"] is False
    assert body["can_toggle_restart_production"] is False


def test_toggle_staging_view_permission_flips_flag(client_factory, db_session):
    sa = make_user(db_session, email="sa5@example.com", role=Role.SUPER_ADMIN)
    target = make_user(db_session, email="target5@example.com", role=Role.USER, can_view_staging=False)
    client = client_factory(user=sa)

    resp = client.post(f"/users/{target.id}/staging-view-toggle", headers={"Accept": "application/json"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["can_view_staging"] is True
    db_session.refresh(target)
    assert target.can_view_staging is True
    assert target.can_toggle_staging is False, "Bat quyen doc KHONG duoc tu dong cap luon quyen ghi"


def test_toggle_production_view_permission_independent_of_staging_view(client_factory, db_session):
    sa = make_user(db_session, email="sa6@example.com", role=Role.SUPER_ADMIN)
    target = make_user(
        db_session, email="target6@example.com", role=Role.USER, can_view_staging=True, can_view_production=False
    )
    client = client_factory(user=sa)

    resp = client.post(f"/users/{target.id}/production-view-toggle", headers={"Accept": "application/json"})

    assert resp.status_code == 200
    assert resp.json()["can_view_production"] is True
    db_session.refresh(target)
    assert target.can_view_production is True
    assert target.can_view_staging is True  # khong bi anh huong


def test_toggle_staging_view_permission_denied_for_non_super_admin(client_factory, db_session):
    admin = make_user(db_session, email="admin2@example.com", role=Role.ADMIN)
    target = make_user(db_session, email="target7@example.com", role=Role.USER)
    client = client_factory(user=admin)

    resp = client.post(f"/users/{target.id}/staging-view-toggle", headers={"Accept": "application/json"})

    assert resp.status_code == 403


def test_toggle_staging_restart_permission_flips_flag_independent_of_replicas(client_factory, db_session):
    """Bat quyen Restart Staging KHONG duoc tu dong cap luon quyen sua Replicas
    (can_toggle_staging) - 2 quyen tach biet hoan toan tu 2026-08-18."""
    sa = make_user(db_session, email="sa7@example.com", role=Role.SUPER_ADMIN)
    target = make_user(
        db_session,
        email="target8@example.com",
        role=Role.USER,
        can_toggle_restart_staging=False,
        can_toggle_staging=False,
    )
    client = client_factory(user=sa)

    resp = client.post(f"/users/{target.id}/staging-restart-toggle", headers={"Accept": "application/json"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["can_toggle_restart_staging"] is True
    db_session.refresh(target)
    assert target.can_toggle_restart_staging is True
    assert target.can_toggle_staging is False


def test_toggle_production_restart_permission_independent_of_staging_restart(client_factory, db_session):
    sa = make_user(db_session, email="sa8@example.com", role=Role.SUPER_ADMIN)
    target = make_user(
        db_session,
        email="target9@example.com",
        role=Role.USER,
        can_toggle_restart_staging=True,
        can_toggle_restart_production=False,
    )
    client = client_factory(user=sa)

    resp = client.post(f"/users/{target.id}/production-restart-toggle", headers={"Accept": "application/json"})

    assert resp.status_code == 200
    assert resp.json()["can_toggle_restart_production"] is True
    db_session.refresh(target)
    assert target.can_toggle_restart_production is True
    assert target.can_toggle_restart_staging is True  # khong bi anh huong


def test_toggle_staging_restart_permission_denied_for_non_super_admin(client_factory, db_session):
    admin = make_user(db_session, email="admin3@example.com", role=Role.ADMIN)
    target = make_user(db_session, email="target10@example.com", role=Role.USER)
    client = client_factory(user=admin)

    resp = client.post(f"/users/{target.id}/staging-restart-toggle", headers={"Accept": "application/json"})

    assert resp.status_code == 403
