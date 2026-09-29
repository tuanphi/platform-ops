"""Kiểm thử model quyền trong app/models.py: Role user/admin/super_admin +
grant theo Group (group_admin_access) / theo Project (project_admin_access)."""

from app.models import Group, Project, Role
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


def test_plain_user_cannot_confirm_or_run_without_grant(db_session):
    user = make_user(db_session, role=Role.USER)

    assert user.can_approve("core", "api") is False
    assert user.can_run("core", "api") is False
    assert user.can_view_all_tasks is False


def test_admin_can_confirm_and_run_any_project(db_session):
    """THAY ĐỔI HÀNH VI CÓ CHỦ Ý (yêu cầu chủ dự án): Admin (không phải Super Admin,
    không cần grant riêng theo Group/Project) giờ được Run/Rollback ở MỌI project/
    application, giống hệt phạm vi Approve/Reject vốn đã có sẵn từ trước. Trước đây
    Admin chỉ Approve/Reject được toàn bộ còn Run bị giới hạn theo grant/Super Admin;
    test này ghi nhận behavior MỚI để phát hiện regression, không phải bug."""
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)

    assert admin.can_approve("core", "api") is True
    assert admin.can_run("core", "api") is True
    assert admin.can_view_all_tasks is True


def test_super_admin_can_confirm_and_run_everything(db_session):
    sa = make_user(db_session, email="sa@example.com", role=Role.SUPER_ADMIN)

    assert sa.can_approve("core", "api") is True
    assert sa.can_run("core", "api") is True
    assert sa.can_manage_settings is True


def test_group_grant_gives_full_access_to_all_projects_in_group(db_session):
    user = make_user(db_session, role=Role.USER)
    group = make_group(db_session, "core")
    user.granted_groups.append(group)
    db_session.commit()

    assert user.can_approve("core", "api") is True
    assert user.can_approve("core", "any-other-app-in-group") is True
    assert user.can_run("core", "api") is True
    assert user.can_view_all_tasks is True
    # nhóm khác không liên quan không được cấp quyền
    assert user.can_approve("other-group", "api") is False


def test_project_grant_only_applies_to_that_exact_application(db_session):
    user = make_user(db_session, role=Role.USER)
    group = make_group(db_session, "core")
    project = make_project(db_session, group, "api")
    user.granted_projects.append(project)
    db_session.commit()

    assert user.can_approve("core", "api") is True
    assert user.can_run("core", "api") is True
    # KHÔNG lan sang app khác cùng group
    assert user.can_approve("core", "worker") is False
    assert user.can_run("core", "worker") is False


def test_project_grant_does_not_leak_to_project_with_same_name_in_other_group(db_session):
    """_has_project_grant so khớp application_name theo p.group.group_name == project_name -
    xác nhận 1 app trùng tên ở group khác không tự động được cấp quyền."""
    user = make_user(db_session, role=Role.USER)
    group_a = make_group(db_session, "group-a")
    group_b = make_group(db_session, "group-b")
    project_a = make_project(db_session, group_a, "shared-app-name")
    make_project(db_session, group_b, "shared-app-name")
    user.granted_projects.append(project_a)
    db_session.commit()

    assert user.can_approve("group-a", "shared-app-name") is True
    assert user.can_approve("group-b", "shared-app-name") is False
