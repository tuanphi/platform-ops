"""HAPPY PATH cho fix UI /tasks: nut bulk Reject/Approve/Run/Cancel/Delete tren toolbar
phai bat/tat theo QUYEN cua user doi voi tung task dang tick, khong con bat het chi vi
co tick chon.

Fix o app/templates/task_list.html: moi checkbox .task-select nay co them data-attribute
data-can-cancel / data-can-approve / data-can-run / data-can-delete, tinh tu
user.can_approve()/can_run()/can_delete_task + not_terminal. JS refresh() doc truc tiep
cac cờ nay tu DOM (khong con bat toan bo nut chi vi >=1 checkbox duoc tick).

Vi day la hanh vi JS phia client (khong co JS runtime trong pytest), file nay CHI kiem
duoc tang render HTML: xac nhan cac data-can-* attribute duoc server render dung theo
quyen thuc te cua user + trang thai task, va xac nhan JS refresh() co doc cac cờ nay
(qua grep noi dung template) - KHONG the mo phong click chuot/tick checkbox that su.
"""

import re

from app.models import DeployTask, Role, TaskStatus
from tests.conftest import make_user


def make_task(db_session, status=TaskStatus.TASK, **kwargs):
    defaults = dict(project="core", application="api", commitid="abc1234", status=status, email="owner@example.com")
    defaults.update(kwargs)
    task = DeployTask(**defaults)
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


def extract_row_html(resp_text: str, task_id: int) -> str:
    """Cat rieng block <tr data-id="{task_id}" ...> ... </tr> tuong ung, tranh false
    positive khi trang co nhieu task/nhieu checkbox."""
    match = re.search(
        rf'<tr data-id="{task_id}"[^>]*>(.*?)</tr>', resp_text, re.DOTALL
    )
    assert match, f"Khong tim thay row cua task id={task_id} trong HTML tra ve"
    return match.group(1)


def extract_checkbox_attrs(row_html: str) -> dict:
    match = re.search(r'<input type="checkbox" class="task-select"[^>]*>', row_html, re.DOTALL)
    assert match, "Khong tim thay checkbox .task-select trong row"
    checkbox_html = match.group(0)
    attrs = {}
    for attr in ("data-can-cancel", "data-can-approve", "data-can-run", "data-can-delete"):
        m = re.search(rf'{re.escape(attr)}="([^"]*)"', checkbox_html)
        attrs[attr] = m.group(1) if m else None
    attrs["disabled"] = "disabled" in checkbox_html
    return attrs


def test_admin_role_sees_can_approve_and_can_run_flags_on_not_terminal_task(client_factory, db_session):
    """TC1: user role ADMIN (full quyen approve/run moi project) xem 1 task not_terminal
    (status=Task, khong phai chu task) -> checkbox row phai co data-can-approve="1" va
    data-can-run="1" (can_approve/can_run tra True cho ADMIN o moi project/application)."""
    admin = make_user(db_session, email="admin@example.com", role=Role.ADMIN)
    task = make_task(
        db_session, status=TaskStatus.TASK, project="core", application="api", email="someone-else@example.com"
    )

    client = client_factory(user=admin)
    resp = client.get("/tasks")

    assert resp.status_code == 200
    attrs = extract_checkbox_attrs(extract_row_html(resp.text, task.id))
    assert attrs["data-can-approve"] == "1"
    assert attrs["data-can-run"] == "1"
    assert attrs["data-can-cancel"] == "1"  # ADMIN cung thoa can_approve -> can_cancel True
    assert attrs["disabled"] is False


def test_plain_user_owner_of_not_terminal_task_has_no_approve_run_flag(client_factory, db_session):
    """TC2: user thuong (role USER, khong co grant Group/Project nao) xem chinh task
    not_terminal do minh tao (email == user.email) -> checkbox KHONG bi disabled (duoc
    chon de Cancel) nhung data-can-approve/data-can-run phai RONG ("") vi khong co quyen
    approve/run; data-can-cancel="1" vi la chu task."""
    plain_user = make_user(db_session, email="plain@example.com", role=Role.USER)
    task = make_task(
        db_session, status=TaskStatus.TASK, project="core", application="api", email=plain_user.email
    )

    client = client_factory(user=plain_user)
    resp = client.get("/tasks")

    assert resp.status_code == 200
    attrs = extract_checkbox_attrs(extract_row_html(resp.text, task.id))
    assert attrs["data-can-approve"] == ""
    assert attrs["data-can-run"] == ""
    assert attrs["data-can-cancel"] == "1"
    assert attrs["disabled"] is False


def test_terminal_task_has_all_action_flags_empty(client_factory, db_session):
    """TC3: task o trang thai terminal (Done) -> data-can-approve/data-can-run/
    data-can-cancel deu RONG du user xem la ADMIN (full quyen approve/run tren moi
    project), vi not_terminal = False chan het cac cờ hanh dong nay."""
    admin = make_user(db_session, email="admin2@example.com", role=Role.ADMIN)
    task = make_task(
        db_session, status=TaskStatus.DONE, project="core", application="api", email="owner@example.com"
    )

    client = client_factory(user=admin)
    resp = client.get("/tasks")

    assert resp.status_code == 200
    attrs = extract_checkbox_attrs(extract_row_html(resp.text, task.id))
    assert attrs["data-can-approve"] == ""
    assert attrs["data-can-run"] == ""
    assert attrs["data-can-cancel"] == ""


def test_js_refresh_reads_permission_flags_from_dom_not_just_checked_count():
    """TC4: xac nhan phan JS refresh() da duoc sua de doc cờ quyen (data-can-approve/
    data-can-run/data-can-cancel/data-can-delete) qua dataset thay vi chi dem so
    checkbox dang tick bat ky. Chi kiem tra su hien dien cua logic (khong chay JS that
    trong pytest)."""
    with open("app/templates/task_list.html", encoding="utf-8") as f:
        html = f.read()

    assert "data-can-cancel=" in html
    assert "data-can-approve=" in html
    assert "data-can-run=" in html
    assert "data-can-delete=" in html
    assert "countCheckedWithFlag" in html
    assert "countCheckedWithFlag('canCancel')" in html
    assert "countCheckedWithFlag('canApprove')" in html
    assert "countCheckedWithFlag('canRun')" in html
    assert "countCheckedWithFlag('canDelete')" in html
