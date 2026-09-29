"""Kiểm thử GET /tasks/api/statuses (app/routers/tasks_router.py::task_statuses), phần
backend của tính năng "Auto-refresh task list status via lightweight JSON polling"
(commit 0d412f8).

Trọng tâm:
  A. Chức năng cơ bản của endpoint (đúng id/status, không lộ field thừa, parse ids an
     toàn với input rác/rỗng/quá dài, giới hạn 200 id).
  B. Bảo mật scope (IDOR) - endpoint PHẢI áp dụng đúng _view_scope_filter như list_tasks:
     user grant hẹp không được thấy task ngoài phạm vi (kể cả khi chủ động hỏi đúng id
     đó), full-access role (Admin/Super Admin) xem được mọi id hỏi, chưa đăng nhập bị
     chặn.

KHÔNG đụng tới app/services/task_service.py, tests/test_task_service_lifecycle.py, hay
tests/test_router_integration.py (agent khác đang sửa song song) - file test MỚI hoàn
toàn, tái dùng fixture/helper của conftest.py và cùng convention với test_view_scope.py.
"""

from app.models import DeployTask, Group, Project, Role, TaskStatus
from tests.conftest import make_user

ENDPOINT = "/tasks/api/statuses"


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
# A. Chức năng endpoint
# ---------------------------------------------------------------------------


def test_valid_ids_in_scope_return_correct_id_and_status(client_factory, db_session):
    owner = make_user(db_session, email="owner@example.com", role=Role.USER)
    t1 = make_task(db_session, email=owner.email, status=TaskStatus.TASK, commitid="a0000001")
    t2 = make_task(db_session, email=owner.email, status=TaskStatus.APPROVED, commitid="a0000002")

    client = client_factory(user=owner)
    resp = client.get(ENDPOINT, params={"ids": f"{t1.id},{t2.id}"})

    assert resp.status_code == 200
    data = resp.json()
    by_id = {item["id"]: item["status"] for item in data}
    assert by_id == {t1.id: "Task", t2.id: "Approved"}


def test_status_change_in_db_is_reflected_on_next_call(client_factory, db_session):
    """Mô phỏng scheduler đổi Running -> Done ở nền: gọi endpoint lần 1 thấy status cũ,
    đổi trực tiếp trong DB (không qua HTTP, giống job nền), gọi lại lần 2 phải thấy
    status MỚI ngay lập tức (không cache)."""
    owner = make_user(db_session, email="owner@example.com", role=Role.USER)
    task = make_task(db_session, email=owner.email, status=TaskStatus.RUNNING, commitid="b0000001")

    client = client_factory(user=owner)
    resp1 = client.get(ENDPOINT, params={"ids": str(task.id)})
    assert resp1.json() == [{"id": task.id, "status": "Running"}]

    task.status = TaskStatus.DONE
    db_session.add(task)
    db_session.commit()

    resp2 = client.get(ENDPOINT, params={"ids": str(task.id)})
    assert resp2.json() == [{"id": task.id, "status": "Done"}]


def test_response_only_contains_id_and_status_no_extra_fields(client_factory, db_session):
    owner = make_user(db_session, email="owner@example.com", role=Role.USER)
    task = make_task(
        db_session,
        email=owner.email,
        commitid="c0000001",
        config_env="DB_PASSWORD=super-secret-value",
        updatefor="something-sensitive",
    )

    client = client_factory(user=owner)
    resp = client.get(ENDPOINT, params={"ids": str(task.id)})

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert set(data[0].keys()) == {"id", "status"}
    # Kiem tra ca body raw, khong chi keys parse duoc - phong truong hop serialize khac.
    assert "super-secret-value" not in resp.text
    assert "config_env" not in resp.text
    assert "email" not in resp.text
    assert "project" not in resp.text
    assert "commitid" not in resp.text


def test_empty_ids_param_returns_empty_list_no_crash(client_factory, db_session):
    user = make_user(db_session)
    client = client_factory(user=user)
    resp = client.get(ENDPOINT, params={"ids": ""})
    assert resp.status_code == 200
    assert resp.json() == []


def test_missing_ids_param_returns_empty_list_no_crash(client_factory, db_session):
    user = make_user(db_session)
    client = client_factory(user=user)
    resp = client.get(ENDPOINT)
    assert resp.status_code == 200
    assert resp.json() == []


def test_all_garbage_ids_ignored_no_crash(client_factory, db_session):
    user = make_user(db_session)
    client = client_factory(user=user)
    resp = client.get(ENDPOINT, params={"ids": "abc,1;drop table users,,null,NaN,1.5"})
    assert resp.status_code == 200
    assert resp.json() == []


def test_negative_and_zero_ids_do_not_crash(client_factory, db_session):
    user = make_user(db_session)
    client = client_factory(user=user)
    resp = client.get(ENDPOINT, params={"ids": "-5,0,-999999"})
    assert resp.status_code == 200
    assert resp.json() == []


def test_extremely_large_id_does_not_crash(client_factory, db_session):
    user = make_user(db_session)
    client = client_factory(user=user)
    huge = "9" * 100
    resp = client.get(ENDPOINT, params={"ids": huge})
    assert resp.status_code == 200
    assert resp.json() == []


def test_mixed_valid_and_garbage_ids_filters_correctly(client_factory, db_session):
    owner = make_user(db_session, email="owner@example.com", role=Role.USER)
    task = make_task(db_session, email=owner.email, commitid="d0000001")

    client = client_factory(user=owner)
    resp = client.get(ENDPOINT, params={"ids": f"abc,{task.id},-1,,9999999999999999999"})
    assert resp.status_code == 200
    assert resp.json() == [{"id": task.id, "status": "Task"}]


def test_more_than_200_ids_are_truncated_to_200(client_factory, db_session):
    owner = make_user(db_session, email="owner@example.com", role=Role.USER)
    # Tao 205 task lien tiep cua chinh owner (trong scope), hoi toan bo id + vai id rac
    # xen giua de kiem tra gioi han 200 KHONG bi pha boi ky tu rac.
    tasks = [make_task(db_session, email=owner.email, commitid=f"e{i:07d}") for i in range(205)]
    ids_str = ",".join(str(t.id) for t in tasks)

    client = client_factory(user=owner)
    resp = client.get(ENDPOINT, params={"ids": ids_str})
    assert resp.status_code == 200
    data = resp.json()
    # Endpoint dung `break` ngay khi parsed_ids dat 200 phan tu HOP LE (200 id dau tien
    # theo thu tu trong chuoi ids) - filter DB tren dung <=200 id do, nen response tra
    # ve <=200 dong.
    assert len(data) <= 200
    returned_ids = {item["id"] for item in data}
    expected_first_200 = {t.id for t in tasks[:200]}
    assert returned_ids == expected_first_200


def test_very_long_ids_string_with_many_garbage_entries_does_not_crash(client_factory, db_session):
    user = make_user(db_session)
    client = client_factory(user=user)
    # >200 phan tu, toan bo la rac (khong phai so) - dam bao vong lap khong bi cham/crash
    # va gioi han 200 chi ap dung cho id HOP LE (0 o day).
    garbage = ",".join(f"garbage-{i}" for i in range(500))
    resp = client.get(ENDPOINT, params={"ids": garbage})
    assert resp.status_code == 200
    assert resp.json() == []


def test_ids_with_leading_trailing_whitespace_and_plus_sign_parsed_or_ignored_safely(client_factory, db_session):
    owner = make_user(db_session, email="owner@example.com", role=Role.USER)
    task = make_task(db_session, email=owner.email, commitid="f0000001")
    client = client_factory(user=owner)
    resp = client.get(ENDPOINT, params={"ids": f" {task.id} , {task.id}  ,  "})
    assert resp.status_code == 200
    assert resp.json() == [{"id": task.id, "status": "Task"}]


def test_duplicate_ids_do_not_produce_duplicate_rows(client_factory, db_session):
    owner = make_user(db_session, email="owner@example.com", role=Role.USER)
    task = make_task(db_session, email=owner.email, commitid="g0000001")
    client = client_factory(user=owner)
    resp = client.get(ENDPOINT, params={"ids": f"{task.id},{task.id},{task.id}"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == task.id


def test_nonexistent_task_id_silently_omitted(client_factory, db_session):
    user = make_user(db_session)
    client = client_factory(user=user)
    resp = client.get(ENDPOINT, params={"ids": "999999"})
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# B. Bảo mật scope (IDOR) - PHẦN QUAN TRỌNG NHẤT
# ---------------------------------------------------------------------------


def test_narrow_project_grant_excludes_out_of_scope_task_from_response(client_factory, db_session):
    """User A chi duoc grant 1 project cu the. Hoi ca id trong scope lan ngoai scope ->
    response CHI chua task trong scope, task ngoai scope bi loai HOAN TOAN (khong co id
    do trong response, khong lo su ton tai)."""
    group_core = make_group(db_session, "core")
    group_other = make_group(db_session, "other")
    project_core_api = make_project(db_session, group_core, "api")
    make_project(db_session, group_other, "worker")

    grantee = make_user(db_session, email="grantee@example.com", role=Role.USER)
    grantee.granted_projects.append(project_core_api)
    db_session.commit()

    stranger = make_user(db_session, email="stranger@example.com", role=Role.USER)
    in_scope_task = make_task(db_session, project="core", application="api", email=stranger.email, commitid="inscope1")
    out_scope_task = make_task(
        db_session, project="other", application="worker", email=stranger.email, commitid="outscope1"
    )

    client = client_factory(user=grantee)
    resp = client.get(ENDPOINT, params={"ids": f"{in_scope_task.id},{out_scope_task.id}"})

    assert resp.status_code == 200
    data = resp.json()
    returned_ids = {item["id"] for item in data}
    assert returned_ids == {in_scope_task.id}
    assert out_scope_task.id not in returned_ids


def test_asking_only_out_of_scope_id_returns_empty_list(client_factory, db_session):
    """Neu CHI hoi id ngoai scope -> response phai rong hoan toan, khong duoc tra ve
    bat ky thong tin nao (ke ca status) tiet lo task do ton tai."""
    group_core = make_group(db_session, "core")
    group_other = make_group(db_session, "other")
    project_core_api = make_project(db_session, group_core, "api")
    make_project(db_session, group_other, "worker")

    grantee = make_user(db_session, email="grantee@example.com", role=Role.USER)
    grantee.granted_projects.append(project_core_api)
    db_session.commit()

    stranger = make_user(db_session, email="stranger@example.com", role=Role.USER)
    out_scope_task = make_task(
        db_session, project="other", application="worker", email=stranger.email, commitid="outscope2"
    )

    client = client_factory(user=grantee)
    resp = client.get(ENDPOINT, params={"ids": str(out_scope_task.id)})

    assert resp.status_code == 200
    assert resp.json() == []


def test_narrow_group_grant_excludes_other_group_task(client_factory, db_session):
    group_core = make_group(db_session, "core")
    make_group(db_session, "other")

    grantee = make_user(db_session, email="grantee@example.com", role=Role.USER)
    grantee.granted_groups.append(group_core)
    db_session.commit()

    stranger = make_user(db_session, email="stranger@example.com", role=Role.USER)
    out_scope_task = make_task(db_session, project="other", application="x", email=stranger.email, commitid="othgrp1")

    client = client_factory(user=grantee)
    resp = client.get(ENDPOINT, params={"ids": str(out_scope_task.id)})
    assert resp.status_code == 200
    assert resp.json() == []


def test_owner_without_grant_can_see_own_task_status(client_factory, db_session):
    owner = make_user(db_session, email="owner-no-grant@example.com", role=Role.USER)
    task = make_task(db_session, email=owner.email, commitid="ownnogrt")

    client = client_factory(user=owner)
    resp = client.get(ENDPOINT, params={"ids": str(task.id)})
    assert resp.status_code == 200
    assert resp.json() == [{"id": task.id, "status": "Task"}]


def test_admin_full_access_sees_any_task_status(client_factory, db_session):
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    u1 = make_user(db_session, email="u1@example.com", role=Role.USER)
    u2 = make_user(db_session, email="u2@example.com", role=Role.USER)
    t1 = make_task(db_session, project="core", application="api", email=u1.email, commitid="admall01")
    t2 = make_task(db_session, project="other", application="worker", email=u2.email, commitid="admall02")

    client = client_factory(user=admin)
    resp = client.get(ENDPOINT, params={"ids": f"{t1.id},{t2.id}"})

    assert resp.status_code == 200
    returned_ids = {item["id"] for item in resp.json()}
    assert returned_ids == {t1.id, t2.id}


def test_super_admin_full_access_sees_any_task_status(client_factory, db_session):
    sa = make_user(db_session, email="sa@example.com", role=Role.SUPER_ADMIN)
    u1 = make_user(db_session, email="u1@example.com", role=Role.USER)
    task = make_task(db_session, project="unrelated-group", application="unrelated-app", email=u1.email)

    client = client_factory(user=sa)
    resp = client.get(ENDPOINT, params={"ids": str(task.id)})

    assert resp.status_code == 200
    assert resp.json() == [{"id": task.id, "status": "Task"}]


def test_unauthenticated_request_is_redirected_not_given_data(client_factory, db_session):
    """Khong co session hop le -> phai bi chan giong het cac route /tasks khac
    (RedirectToLogin -> 303 toi /auth/login), KHONG bao gio tra ve JSON du lieu."""
    client = client_factory(user=None)
    resp = client.get(ENDPOINT, params={"ids": "1"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_ids_query_injection_style_payload_does_not_crash_or_leak(client_factory, db_session):
    """Nghi van bao mat can [sec] xac nhan muc do (khong tu ket luan o day): dam bao chuoi
    kieu SQL-injection payload trong `ids` KHONG lam crash endpoint va KHONG tra ve du
    lieu ngoai y muon - vi input duoc parse bang int() an toan (ValueError bi bat), ve ly
    thuyet khong the cham toi SQL truc tiep, nhung van kiem tra hanh vi runtime thuc te."""
    owner = make_user(db_session, email="owner@example.com", role=Role.USER)
    task = make_task(db_session, email=owner.email, commitid="h0000001")
    client = client_factory(user=owner)

    payloads = [
        "1 OR 1=1",
        "1) UNION SELECT * FROM users--",
        "'; DROP TABLE deploy_tasks;--",
        f"{task.id}; SELECT * FROM users",
    ]
    for payload in payloads:
        resp = client.get(ENDPOINT, params={"ids": payload})
        assert resp.status_code == 200, f"payload gay loi 500: {payload!r}"
        # Chi id hop le duoc parse boi int() truoc dau ";"/khoang trang/OR moi duoc giu -
        # cac chuoi tren khong parse duoc thanh int() nguyen ven nen bi bo qua toan bo.
        assert resp.json() == [], f"payload ro ri du lieu khong mong doi: {payload!r} -> {resp.json()}"

    # Sau khi thu payload, endpoint van hoat dong binh thuong cho request hop le tiep theo
    # (khong bi "hong" trang thai DB/app).
    resp_ok = client.get(ENDPOINT, params={"ids": str(task.id)})
    assert resp_ok.status_code == 200
    assert resp_ok.json() == [{"id": task.id, "status": "Task"}]
