"""Regression test cho bug PRODUCTION: bam Run/Approve/Reject/Cancel o man hinh chi
tiet task -> "Co loi xay ra, thu lai sau" (400 Bad Request, log server "Did not find CR
at end of boundary"). Nguyen nhan: 4 form POST trong task_detail.html co class
"ajax-form" nhung khong co <input> nao -> FormData rong -> browser sinh multipart body
khong hop le -> python_multipart parse loi -> 400.

Dev da sua 2 diem:
1. task_detail.html: them <input type="hidden" name="next"> vao ca 4 form.
2. base.html doAjaxSubmit(): doi `body: new FormData(form)` -> `body: new
   URLSearchParams(new FormData(form))` (gui application/x-www-form-urlencoded, an toan
   ke ca form rong).

File nay CHI THEM test - khong sua code app/."""

from __future__ import annotations

import re

from app.models import Role, TaskStatus
from tests.conftest import make_user
from tests.test_run_rollback_rate_limit import make_task


# ---------------------------------------------------------------------------
# Nhiem vu 1 - chot chan cap template: ca 4 form ajax-form trong task_detail.html phai
# co <input type="hidden" name="next"> de FormData KHONG BAO GIO rong khi submit qua
# doAjaxSubmit().
# ---------------------------------------------------------------------------


def test_task_detail_all_4_ajax_forms_have_hidden_next_input(client_factory, db_session):
    admin = make_user(db_session, email="viewer-admin@example.com", role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.TASK, commitid="tpl00001")

    client = client_factory(user=admin)
    resp = client.get(f"/tasks/{task.id}")
    assert resp.status_code == 200
    html = resp.text

    # Tach tung khoi <form ...>...</form> co class chua "ajax-form", kiem tra MOI khoi
    # deu co input hidden name="next".
    forms = re.findall(r"<form\b[^>]*class=\"[^\"]*ajax-form[^\"]*\"[^>]*>.*?</form>", html, re.S)
    assert len(forms) == 4, f"Ky vong dung 4 form ajax-form (cancel/reject/approve/run), thuc te {len(forms)}"

    actions = []
    for form in forms:
        assert re.search(r'<input\s+type="hidden"\s+name="next"', form), (
            f"Form thieu <input type=\"hidden\" name=\"next\"> -> FormData co the rong -> tai dien bug 400:\n{form}"
        )
        action_match = re.search(r'action="([^"]+)"', form)
        actions.append(action_match.group(1) if action_match else None)

    for suffix in ("cancel", "reject", "approve", "run"):
        assert any(a and a.endswith(f"/{suffix}") for a in actions), f"Thieu form action .../{suffix}"


def test_task_detail_next_hidden_input_value_matches_current_url_path(client_factory, db_session):
    admin = make_user(db_session, email="viewer-admin2@example.com", role=Role.ADMIN)
    task = make_task(db_session, status=TaskStatus.TASK, commitid="tpl00002")

    client = client_factory(user=admin)
    resp = client.get(f"/tasks/{task.id}")
    assert resp.status_code == 200
    assert f'<input type="hidden" name="next" value="/tasks/{task.id}">' in resp.text


# ---------------------------------------------------------------------------
# Nhiem vu 2 - tai hien loi goc (server-side): FormData rong -> multipart body khong co
# phan nao ca -> python_multipart bao "Did not find CR at end of boundary" -> 400.
#
# QUAN TRONG: day la hanh vi CUA THU VIEN python_multipart o TANG SERVER, khong lien
# quan gi toi HTML template hay JS phia client. Ban va cua Dev sua o CLIENT (base.html
# doi sang application/x-www-form-urlencoded) de KHONG BAO GIO gui multipart rong nua -
# server van se tra 400 neu co ai/cai gi gui multipart rong toi endpoint nay (vd script,
# tool debug, hoac browser cu khong chay duoc JS moi). Do la hanh vi ky vong CUA
# python_multipart, KHONG PHAI loi cua Dev - test nay chi de xac nhan/ghi lai hanh vi do
# (khong danh gia PASS/FAIL cho phan sua cua Dev).
# ---------------------------------------------------------------------------


def test_raw_multipart_empty_formdata_body_still_400_at_server_but_this_is_not_a_dev_bug(
    client_factory, db_session, caplog
):
    user = make_user(db_session, email="repro-owner@example.com", role=Role.USER)
    task = make_task(db_session, status=TaskStatus.TASK, email="repro-owner@example.com", commitid="rpr0001")

    client = client_factory(user=user)

    boundary = "----WebKitFormBoundaryEmpty"
    # Mo phong dung cai browser tung sinh ra khi `new FormData(form)` khong co entry nao
    # (form chi co <button> khong co name) va duoc dua thang vao fetch(body: formdata).
    empty_multipart_body = f"--{boundary}--\r\n".encode()

    with caplog.at_level("WARNING"):
        resp = client.post(
            f"/tasks/{task.id}/cancel",
            content=empty_multipart_body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json",
            },
            follow_redirects=False,
        )

    assert resp.status_code == 400, (
        "Server (python_multipart) van tra 400 cho multipart hoan toan rong - dung nhu log "
        "loi goc tren production. Day la hanh vi cua thu vien parser, KHONG PHAI diem Dev "
        "can sua (Dev da sua o client: base.html khong con gui multipart rong nua)."
    )
    assert "boundary" in caplog.text.lower() or "error parsing" in resp.text.lower()

    # Task KHONG bi thay doi trang thai vi request bi tu choi truoc khi vao route handler.
    db_session.refresh(task)
    assert task.status == TaskStatus.TASK


# ---------------------------------------------------------------------------
# Nhiem vu 3 - them "next" khong pha vo hanh vi hien co: redirect van SACH (khong
# ?ok=/msg=) va guard chong open-redirect van chan next tro ra ngoai.
# ---------------------------------------------------------------------------


def test_cancel_from_task_detail_redirects_to_clean_next_url(client_factory, db_session):
    owner = make_user(db_session, email="next-owner@example.com", role=Role.USER)
    task = make_task(db_session, status=TaskStatus.TASK, email="next-owner@example.com", commitid="nxt0010")

    client = client_factory(user=owner)
    resp = client.post(
        f"/tasks/{task.id}/cancel",
        data={"next": f"/tasks/{task.id}"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location == f"/tasks/{task.id}"
    assert "ok=" not in location
    assert "msg=" not in location


def test_open_redirect_guard_rejects_external_next_and_falls_back_to_task_detail(client_factory, db_session):
    owner = make_user(db_session, email="next-evil@example.com", role=Role.USER)
    task = make_task(db_session, status=TaskStatus.TASK, email="next-evil@example.com", commitid="nxt0020")

    client = client_factory(user=owner)
    resp = client.post(
        f"/tasks/{task.id}/cancel",
        data={"next": "//evil.com/phish"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location == f"/tasks/{task.id}", (
        f"_is_safe_next() phai chan next='//evil.com/phish' (protocol-relative URL, van bi trinh "
        f"duyet coi la tro ra ngoai) va fallback ve /tasks/{{id}}, thuc te redirect toi: {location}"
    )
    assert not location.startswith("//")


def test_open_redirect_guard_unit_rejects_various_unsafe_next_values():
    from app.routers.actions_router import _is_safe_next

    assert _is_safe_next(None) is False
    assert _is_safe_next("") is False
    assert _is_safe_next("//evil.com") is False
    assert _is_safe_next("https://evil.com") is False
    assert _is_safe_next("http://evil.com/x") is False
    # path noi bo hop le van duoc chap nhan
    assert _is_safe_next("/tasks/1") is True
    assert _is_safe_next("/tasks?status=TASK&page=2") is True
