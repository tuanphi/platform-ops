"""HAPPY PATH cho thay đổi filter `/tasks`: param `email` -> `q` (tìm kiếm tổng quát
trên project/application/commitid/email/image_version/updatefor + id nếu q là số).

Theo yêu cầu leader/dev: `q` áp dụng CỘNG THÊM (AND) sau `_view_scope_filter`, không
được thay thế/vượt qua view-scope của user thường (xem tests/test_view_scope.py để biết
bối cảnh lỗ hổng HIGH đã fix)."""

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


def test_q_matches_by_project_returns_multiple_matching_tasks(client_factory, db_session):
    """TC1: `/tasks?q=<project>` khớp NHIỀU task cùng project -> trả đủ cả hai."""
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    make_task(db_session, project="payment-core", application="api", commitid="pay0001a")
    make_task(db_session, project="payment-core", application="worker", commitid="pay0002b")
    make_task(db_session, project="unrelated", application="x", commitid="unrel001")

    client = client_factory(user=admin)
    resp = client.get("/tasks?q=payment-core")

    assert resp.status_code == 200
    assert "pay0001a" in resp.text
    assert "pay0002b" in resp.text
    assert "unrel001" not in resp.text


def test_q_matches_by_commitid(client_factory, db_session):
    """TC2: `/tasks?q=<commitid>` lọc đúng theo commit."""
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    make_task(db_session, commitid="deadbeef", project="core", application="api")
    make_task(db_session, commitid="cafef00d", project="core", application="api2")

    client = client_factory(user=admin)
    resp = client.get("/tasks?q=deadbeef")

    assert resp.status_code == 200
    assert "deadbeef" in resp.text
    assert "cafef00d" not in resp.text


def test_q_matches_by_numeric_id(client_factory, db_session):
    """TC3: `/tasks?q=<id>` (chuỗi số) khớp theo DeployTask.id."""
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    task = make_task(db_session, commitid="idmatch1", project="core", application="api")

    client = client_factory(user=admin)
    resp = client.get(f"/tasks?q={task.id}")

    assert resp.status_code == 200
    assert "idmatch1" in resp.text


def test_status_filter_still_works(client_factory, db_session):
    """TC4: `/tasks?status=<status>` vẫn lọc đúng (không bị phá bởi việc đổi email->q)."""
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    make_task(db_session, status=TaskStatus.DONE, commitid="donetask")
    make_task(db_session, status=TaskStatus.TASK, commitid="tasktask")

    client = client_factory(user=admin)
    resp = client.get("/tasks?status=Done")

    assert resp.status_code == 200
    assert "donetask" in resp.text
    assert "tasktask" not in resp.text


def test_narrow_grant_user_q_search_does_not_leak_out_of_scope_task(client_factory, db_session):
    """TC5 (view-scope + q): user thường được grant hẹp 1 Project, dùng `q` khớp
    đúng nội dung của task THUỘC project/group KHÁC -> vẫn KHÔNG được thấy task đó.
    `q` chỉ CỘNG THÊM sau _view_scope_filter, không được dùng để bypass."""
    group_core = make_group(db_session, "core")
    group_other = make_group(db_session, "other")
    project_core_api = make_project(db_session, group_core, "api")
    make_project(db_session, group_other, "worker")

    grantee = make_user(db_session, email="grantee@example.com", role=Role.USER)
    grantee.granted_projects.append(project_core_api)
    db_session.commit()

    stranger = make_user(db_session, email="stranger@example.com", role=Role.USER)
    make_task(
        db_session,
        project="other",
        application="worker",
        email=stranger.email,
        commitid="secretout",
    )

    client = client_factory(user=grantee)
    resp = client.get("/tasks?q=secretout")

    assert resp.status_code == 200
    # Luu y: gia tri q duoc echo lai vao input search (value="secretout"), nen KHONG
    # the assert "secretout" not in resp.text mot cach ngay tho - phai loai tru dung
    # o dang table cell (data-label="Commit ID") de tranh false positive.
    assert '<td data-label="Commit ID">secretout</td>' not in resp.text


def test_q_huge_numeric_string_does_not_500(client_factory, db_session):
    """TC7 (regression fix MEDIUM): `/tasks?q=<100 chu so>` truoc day gay HTTP 500
    (OverflowError vuot BIGINT o driver DB khi so sanh DeployTask.id == q_as_id).
    Sau ban va: guard 0 < q_as_id <= 2**63-1 phai tra 200, bo qua dieu kien id an toan."""
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    make_task(db_session, commitid="stillhere", project="core", application="api")

    client = client_factory(user=admin)
    resp = client.get(f"/tasks?q={'9' * 100}")

    assert resp.status_code == 200
    # q khong khop bat ky truong text nao va dieu kien id bi bo qua (vuot pham vi) ->
    # van tra ve trang 200 hop le, khong con task nao khop nhung khong crash.
    assert "stillhere" not in resp.text


def test_q_unicode_digit_does_not_500(client_factory, db_session):
    """TC8 (regression fix MEDIUM): `/tasks?q=²` (ky tu Unicode co isdigit()==True
    nhung int() raise ValueError) truoc day gay HTTP 500. Sau ban va phai tra 200."""
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    make_task(db_session, commitid="stillhere2", project="core", application="api")

    client = client_factory(user=admin)
    resp = client.get("/tasks?q=%C2%B2")  # "²" URL-encoded (UTF-8: C2 B2)

    assert resp.status_code == 200


def test_narrow_grant_user_q_search_finds_in_scope_task(client_factory, db_session):
    """TC6 (view-scope + q, positive case): task trong đúng phạm vi grant vẫn tìm
    được qua `q` (đảm bảo AND filter không xoá luôn kết quả hợp lệ)."""
    group_core = make_group(db_session, "core")
    project_core_api = make_project(db_session, group_core, "api")

    grantee = make_user(db_session, email="grantee@example.com", role=Role.USER)
    grantee.granted_projects.append(project_core_api)
    db_session.commit()

    make_task(
        db_session,
        project="core",
        application="api",
        email=grantee.email,
        commitid="inscope99",
    )

    client = client_factory(user=grantee)
    resp = client.get("/tasks?q=inscope99")

    assert resp.status_code == 200
    assert "inscope99" in resp.text
