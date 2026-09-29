"""RE-TEST vòng vá lỗ hổng HIGH view-scope (broken access control).

Bối cảnh fix (xem app/models.py::is_full_access_role/can_view_task và
app/routers/tasks_router.py::_view_scope_filter): trước đây `list_tasks`/`task_detail`/
`dashboard` dùng `can_view_all_tasks` (True chỉ vì user CÓ tham gia bất kỳ grant nào) để
quyết định cho xem TOÀN BỘ task - user chỉ được grant 1 Project/Group hẹp vẫn xem được
task của Project/Group khác (kể cả `config_env` chứa secret). Sau fix, chỉ
`is_full_access_role` (Admin/Super Admin) mới xem toàn bộ; user có grant hẹp chỉ được xem
đúng phạm vi qua `_view_scope_filter`/`can_view_task`.

File này bổ sung test khai thác đúng kịch bản đã bị lỗ hổng đó (case 1 = PoC chính, phải
PASS nghĩa là bị chặn đúng) + các case xác nhận fix không phá quyền hợp lệ (owner, group
grant, admin/super_admin full access, dashboard đúng phạm vi)."""

from app.models import DeployTask, Group, Project, Role, TaskStatus
from tests.conftest import make_user


def make_group(db_session, name="core"):
    group = Group(group_name=name, is_active=True)
    db_session.add(group)
    db_session.commit()
    db_session.refresh(group)
    return group


def make_project(db_session, group, name="api"):
    project = Project(group_id=group.id, application_name=name, is_active=True)
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)
    return project


def make_task(db_session, status=TaskStatus.TASK, **kwargs):
    defaults = dict(project="core", application="api", commitid="abc1234", status=status, email="owner@example.com")
    defaults.update(kwargs)
    task = DeployTask(**defaults)
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


# ---------------------------------------------------------------------------
# Case 1 (PoC chính của lỗ hổng HIGH): grant hẹp đúng 1 Project KHÔNG được xem
# task của project/group KHÁC - cả list lẫn detail (không lộ config_env).
# ---------------------------------------------------------------------------


def test_narrow_project_grant_hides_list_tasks_outside_scope(client_factory, db_session):
    group_core = make_group(db_session, "core")
    group_other = make_group(db_session, "other")
    project_core_api = make_project(db_session, group_core, "api")
    make_project(db_session, group_other, "worker")

    grantee = make_user(db_session, email="grantee@example.com", role=Role.USER)
    grantee.granted_projects.append(project_core_api)
    db_session.commit()

    stranger = make_user(db_session, email="stranger@example.com", role=Role.USER)
    make_task(db_session, project="other", application="worker", email=stranger.email, commitid="out0scope")

    client = client_factory(user=grantee)
    resp = client.get("/tasks")

    assert resp.status_code == 200
    assert "out0scope" not in resp.text


def test_narrow_project_grant_blocks_task_detail_outside_scope_and_hides_config_env(client_factory, db_session):
    group_core = make_group(db_session, "core")
    group_other = make_group(db_session, "other")
    project_core_api = make_project(db_session, group_core, "api")
    make_project(db_session, group_other, "worker")

    grantee = make_user(db_session, email="grantee@example.com", role=Role.USER)
    grantee.granted_projects.append(project_core_api)
    db_session.commit()

    stranger = make_user(db_session, email="stranger@example.com", role=Role.USER)
    task = make_task(
        db_session,
        project="other",
        application="worker",
        email=stranger.email,
        config_env="DB_PASSWORD=super-secret-value",
    )

    client = client_factory(user=grantee)
    resp = client.get(f"/tasks/{task.id}")

    assert resp.status_code == 200
    assert "super-secret-value" not in resp.text
    assert "Không tìm thấy" in resp.text


def test_narrow_group_grant_hides_other_group_tasks(client_factory, db_session):
    """Grant theo GROUP ("core") cũng không được rò rỉ sang group khác ("other")."""
    group_core = make_group(db_session, "core")
    make_group(db_session, "other")

    grantee = make_user(db_session, email="grantee@example.com", role=Role.USER)
    grantee.granted_groups.append(group_core)
    db_session.commit()

    stranger = make_user(db_session, email="stranger@example.com", role=Role.USER)
    make_task(db_session, project="other", application="anything", email=stranger.email, commitid="othgrp01")

    client = client_factory(user=grantee)
    resp = client.get("/tasks")

    assert resp.status_code == 200
    assert "othgrp01" not in resp.text


# ---------------------------------------------------------------------------
# Case 2: cùng user grant hẹp VẪN xem được task thuộc ĐÚNG Project được grant
# dù task do người khác tạo (không phá quyền hợp lệ).
# ---------------------------------------------------------------------------


def test_narrow_project_grant_shows_list_task_within_granted_project_created_by_others(client_factory, db_session):
    group_core = make_group(db_session, "core")
    project_core_api = make_project(db_session, group_core, "api")

    grantee = make_user(db_session, email="grantee@example.com", role=Role.USER)
    grantee.granted_projects.append(project_core_api)
    db_session.commit()

    someone_else = make_user(db_session, email="someone_else@example.com", role=Role.USER)
    make_task(db_session, project="core", application="api", email=someone_else.email, commitid="ingrant1")

    client = client_factory(user=grantee)
    resp = client.get("/tasks")

    assert resp.status_code == 200
    assert "ingrant1" in resp.text


def test_narrow_project_grant_shows_full_detail_within_granted_project(client_factory, db_session):
    group_core = make_group(db_session, "core")
    project_core_api = make_project(db_session, group_core, "api")

    grantee = make_user(db_session, email="grantee@example.com", role=Role.USER)
    grantee.granted_projects.append(project_core_api)
    db_session.commit()

    someone_else = make_user(db_session, email="someone_else@example.com", role=Role.USER)
    task = make_task(
        db_session,
        project="core",
        application="api",
        email=someone_else.email,
        config_env="DB_PASSWORD=visible-to-grantee",
    )

    client = client_factory(user=grantee)
    resp = client.get(f"/tasks/{task.id}")

    assert resp.status_code == 200
    assert "visible-to-grantee" in resp.text
    assert "Không tìm thấy" not in resp.text


# ---------------------------------------------------------------------------
# Case 3: grant theo GROUP xem được mọi application trong group đó, nhưng
# không xem được group khác (list + detail).
# ---------------------------------------------------------------------------


def test_group_grant_shows_every_application_in_group_list_and_detail(client_factory, db_session):
    group_core = make_group(db_session, "core")

    grantee = make_user(db_session, email="grantee@example.com", role=Role.USER)
    grantee.granted_groups.append(group_core)
    db_session.commit()

    someone_else = make_user(db_session, email="someone_else@example.com", role=Role.USER)
    task_api = make_task(db_session, project="core", application="api", email=someone_else.email, commitid="grpapi01")
    task_worker = make_task(
        db_session, project="core", application="worker", email=someone_else.email, commitid="grpwrk01"
    )

    client = client_factory(user=grantee)
    resp_list = client.get("/tasks")
    assert resp_list.status_code == 200
    assert "grpapi01" in resp_list.text
    assert "grpwrk01" in resp_list.text

    resp_detail_api = client.get(f"/tasks/{task_api.id}")
    resp_detail_worker = client.get(f"/tasks/{task_worker.id}")
    assert "Không tìm thấy" not in resp_detail_api.text
    assert "Không tìm thấy" not in resp_detail_worker.text


def test_group_grant_still_hides_tasks_of_other_group(client_factory, db_session):
    group_core = make_group(db_session, "core")
    make_group(db_session, "other")

    grantee = make_user(db_session, email="grantee@example.com", role=Role.USER)
    grantee.granted_groups.append(group_core)
    db_session.commit()

    stranger = make_user(db_session, email="stranger@example.com", role=Role.USER)
    task = make_task(
        db_session,
        project="other",
        application="whatever",
        email=stranger.email,
        commitid="othgrp02",
        config_env="SECRET=not-for-grantee",
    )

    client = client_factory(user=grantee)
    resp_list = client.get("/tasks")
    resp_detail = client.get(f"/tasks/{task.id}")

    assert "othgrp02" not in resp_list.text
    assert "not-for-grantee" not in resp_detail.text
    assert "Không tìm thấy" in resp_detail.text


# ---------------------------------------------------------------------------
# Case 4: chủ sở hữu task luôn xem được task của mình dù KHÔNG có grant nào.
# ---------------------------------------------------------------------------


def test_owner_without_any_grant_sees_own_task_in_list_and_detail(client_factory, db_session):
    owner = make_user(db_session, email="owner-no-grant@example.com", role=Role.USER)
    task = make_task(
        db_session,
        project="core",
        application="api",
        email=owner.email,
        commitid="ownnogrt",
        config_env="DB_PASSWORD=owner-secret",
    )
    # task khác project hoàn toàn không liên quan, không phải của owner
    stranger = make_user(db_session, email="stranger2@example.com", role=Role.USER)
    make_task(db_session, project="foreign", application="x", email=stranger.email, commitid="foreign1")

    client = client_factory(user=owner)

    resp_list = client.get("/tasks")
    assert "ownnogrt" in resp_list.text
    assert "foreign1" not in resp_list.text

    resp_detail = client.get(f"/tasks/{task.id}")
    assert "owner-secret" in resp_detail.text
    assert "Không tìm thấy" not in resp_detail.text


# ---------------------------------------------------------------------------
# Case 5: Admin/Super Admin (is_full_access_role) vẫn xem TOÀN BỘ task - fix
# không phá quyền hợp lệ.
# ---------------------------------------------------------------------------


def test_admin_full_access_sees_tasks_across_all_projects_and_groups(client_factory, db_session):
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    u1 = make_user(db_session, email="u1@example.com", role=Role.USER)
    u2 = make_user(db_session, email="u2@example.com", role=Role.USER)
    make_task(db_session, project="core", application="api", email=u1.email, commitid="admall01")
    make_task(db_session, project="other", application="worker", email=u2.email, commitid="admall02")

    client = client_factory(user=admin)
    resp = client.get("/tasks")

    assert resp.status_code == 200
    assert "admall01" in resp.text
    assert "admall02" in resp.text


def test_super_admin_full_access_sees_task_detail_of_any_project(client_factory, db_session):
    sa = make_user(db_session, email="sa@example.com", role=Role.SUPER_ADMIN)
    u1 = make_user(db_session, email="u1@example.com", role=Role.USER)
    task = make_task(
        db_session,
        project="unrelated-group",
        application="unrelated-app",
        email=u1.email,
        config_env="SECRET=visible-to-super-admin",
    )

    client = client_factory(user=sa)
    resp = client.get(f"/tasks/{task.id}")

    assert resp.status_code == 200
    assert "visible-to-super-admin" in resp.text
    assert "Không tìm thấy" not in resp.text


# ---------------------------------------------------------------------------
# Case 6: dashboard - user grant hẹp thấy số liệu ĐÚNG phạm vi được cấp,
# không phải chỉ task của bản thân, cũng không phải toàn bộ hệ thống.
# ---------------------------------------------------------------------------


def _parse_dashboard_counts(html: str) -> dict[str, int]:
    import re

    return {
        status: int(count)
        for count, status in re.findall(r'<div class="num">(\d+)</div>\s*<span class="badge badge-([^"]+)">', html)
    }


def test_dashboard_narrow_grant_scope_counts_only_granted_and_own_tasks(client_factory, db_session):
    group_core = make_group(db_session, "core")
    group_other = make_group(db_session, "other")
    project_core_api = make_project(db_session, group_core, "api")
    make_project(db_session, group_other, "worker")

    grantee = make_user(db_session, email="grantee@example.com", role=Role.USER)
    grantee.granted_projects.append(project_core_api)
    db_session.commit()

    someone_else = make_user(db_session, email="someone_else@example.com", role=Role.USER)

    # 2 task trong pham vi duoc grant (project core/api), tao boi nguoi khac
    make_task(db_session, project="core", application="api", email=someone_else.email, status=TaskStatus.TASK)
    make_task(db_session, project="core", application="api", email=someone_else.email, status=TaskStatus.APPROVED)
    # 1 task cua chinh grantee nhung KHONG thuoc pham vi grant (project/group khac) -
    # van phai duoc tinh vi la chu so huu
    make_task(db_session, project="other", application="worker", email=grantee.email, status=TaskStatus.DONE)
    # 1 task hoan toan ngoai pham vi (khong phai chu, khong duoc grant) - KHONG duoc tinh
    make_task(db_session, project="other", application="worker", email=someone_else.email, status=TaskStatus.TASK)

    client = client_factory(user=grantee)
    resp = client.get("/home")

    assert resp.status_code == 200
    counts = _parse_dashboard_counts(resp.text)
    assert counts.get(TaskStatus.TASK.value) == 1  # chi 1 (trong pham vi), KHONG tinh task ngoai pham vi cua someone_else
    assert counts.get(TaskStatus.APPROVED.value) == 1
    assert counts.get(TaskStatus.DONE.value) == 1  # task cua chinh minh du khac project
    assert sum(counts.values()) == 3  # tong dung 3, khong phai 4 (da loai task ngoai pham vi)


def test_dashboard_admin_counts_all_tasks_regardless_of_grant(client_factory, db_session):
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    u1 = make_user(db_session, email="u1@example.com", role=Role.USER)
    make_task(db_session, project="core", application="api", email=u1.email, status=TaskStatus.TASK)
    make_task(db_session, project="other", application="worker", email=u1.email, status=TaskStatus.DONE)

    client = client_factory(user=admin)
    resp = client.get("/home")

    assert resp.status_code == 200
    counts = _parse_dashboard_counts(resp.text)
    assert sum(counts.values()) == 2
