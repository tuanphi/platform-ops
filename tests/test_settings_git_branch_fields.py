"""Test luong chinh (happy path) cho tinh nang cau hinh ten nhanh git
staging/master ra UI Settings (section=git), co validate khong rong + validate
nhanh ton tai tren remote qua remote_branch_exists().

Chi test 1-3 TC theo yeu cau [TEST] tu Leader - monkeypatch
app.routers.settings_router.remote_branch_exists de tranh goi git that ra
mang (khop convention cua tests/test_run_rollback_rate_limit.py /
tests/test_session_remember_days.py: dung client_factory + make_user).
"""

from app.models import Role
from app.services.app_setting_service import get_app_setting
from tests.conftest import make_user

VALID_PAYLOAD = {
    "section": "git",
    "git_remote_repo_url": "https://gitlab.example.com/devops/helm/applications.git",
    "git_commit_author_email": "form-deploy@g-pay.vn",
    "git_commit_author_name": "form-deploy",
    "git_branch_staging": "staging",
    "git_branch_master": "master",
    "staging_values_file_path": "envValues/values-sandbox.yaml",
    "production_values_file_path": "envValues/values-production.yaml",
}


# ---------------------------------------------------------------------------
# TC1: seed mac dinh tu env - git_branch_staging="staging"/git_branch_master="master"
# ---------------------------------------------------------------------------


def test_seed_default_git_branch_fields(db_session):
    setting = get_app_setting(db_session)

    assert setting.git_branch_staging == "staging"
    assert setting.git_branch_master == "master"


# ---------------------------------------------------------------------------
# TC2: POST section=git voi 2 nhanh hop le (remote_branch_exists mock True) -> luu OK
# ---------------------------------------------------------------------------


def test_update_git_branches_success_when_both_exist_on_remote(client_factory, db_session, monkeypatch):
    # remote_branch_exists duoc import CUC BO (`from app.services.git_service import
    # remote_branch_exists`) BEN TRONG ham update_general moi lan goi - phai monkeypatch
    # dung o module goc app.services.git_service, monkeypatch settings_router.
    # remote_branch_exists se KHONG co tac dung gi (function tu import lai moi lan).
    import app.services.git_service as git_service

    monkeypatch.setattr(git_service, "remote_branch_exists", lambda repo_url, branch: True)

    sa = make_user(db_session, email="sa@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)

    resp = client.post(
        "/settings/general",
        data={**VALID_PAYLOAD, "git_branch_staging": "release/staging", "git_branch_master": "release/master"},
        follow_redirects=False,
        headers={"accept": "application/json"},
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    updated = get_app_setting(db_session)
    assert updated.git_branch_staging == "release/staging"
    assert updated.git_branch_master == "release/master"


# ---------------------------------------------------------------------------
# TC3: reject - nhanh rong KHONG duoc luu, khong goi remote_branch_exists
# ---------------------------------------------------------------------------


def test_update_git_branches_rejected_when_blank(client_factory, db_session, monkeypatch):
    import app.services.git_service as git_service

    called = []
    monkeypatch.setattr(
        git_service, "remote_branch_exists", lambda repo_url, branch: called.append(branch) or True
    )

    sa = make_user(db_session, email="sa@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)
    original_staging = get_app_setting(db_session).git_branch_staging

    resp = client.post(
        "/settings/general",
        data={**VALID_PAYLOAD, "git_branch_staging": "   "},
        follow_redirects=False,
        headers={"accept": "application/json"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert not called, "khong duoc goi remote_branch_exists khi validate rong da that bai truoc"
    assert get_app_setting(db_session).git_branch_staging == original_staging


# ---------------------------------------------------------------------------
# TC3b: reject - nhanh khong ton tai tren remote (mock False) KHONG duoc luu
# ---------------------------------------------------------------------------


def test_update_git_branches_rejected_when_branch_missing_on_remote(client_factory, db_session, monkeypatch):
    import app.services.git_service as git_service

    monkeypatch.setattr(git_service, "remote_branch_exists", lambda repo_url, branch: branch != "no-such-branch")

    sa = make_user(db_session, email="sa@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)
    original = get_app_setting(db_session)
    original_staging, original_master = original.git_branch_staging, original.git_branch_master

    resp = client.post(
        "/settings/general",
        data={**VALID_PAYLOAD, "git_branch_staging": "no-such-branch"},
        follow_redirects=False,
        headers={"accept": "application/json"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "không tồn tại trên remote" in body["message"]
    updated = get_app_setting(db_session)
    assert updated.git_branch_staging == original_staging
    assert updated.git_branch_master == original_master


# ---------------------------------------------------------------------------
# TC4 (regression bao mat - fix CWE-88 git argument injection): ten nhanh bat
# dau bang "-" phai bi chan boi validate_safe_branch_name TRUOC khi goi
# remote_branch_exists (khong duoc de lot xuong subprocess git ls-remote).
# ---------------------------------------------------------------------------


def test_update_git_branches_rejected_when_branch_is_option_injection(client_factory, db_session, monkeypatch):
    import app.services.git_service as git_service

    called = []
    monkeypatch.setattr(
        git_service, "remote_branch_exists", lambda repo_url, branch: called.append(branch) or True
    )

    sa = make_user(db_session, email="sa@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)
    original = get_app_setting(db_session)
    original_staging, original_master = original.git_branch_staging, original.git_branch_master

    resp = client.post(
        "/settings/general",
        data={**VALID_PAYLOAD, "git_branch_staging": "--upload-pack=touch /tmp/x"},
        follow_redirects=False,
        headers={"accept": "application/json"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert not called, "khong duoc goi remote_branch_exists khi validate ten nhanh da that bai truoc"
    updated = get_app_setting(db_session)
    assert updated.git_branch_staging == original_staging
    assert updated.git_branch_master == original_master


def test_update_git_branches_rejected_when_branch_starts_with_dash(client_factory, db_session, monkeypatch):
    """Bien the ngan gon hon ("-x") - van phai bi chan boi cung 1 whitelist regex."""
    import app.services.git_service as git_service

    called = []
    monkeypatch.setattr(
        git_service, "remote_branch_exists", lambda repo_url, branch: called.append(branch) or True
    )

    sa = make_user(db_session, email="sa@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)

    resp = client.post(
        "/settings/general",
        data={**VALID_PAYLOAD, "git_branch_master": "-x"},
        follow_redirects=False,
        headers={"accept": "application/json"},
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert not called


# ---------------------------------------------------------------------------
# TC5 (unit): validate_safe_branch_name - chap nhan ten nhanh hop le, tu choi
# cac bien the injection/ky tu nguy hiem cho git CLI.
# ---------------------------------------------------------------------------


def test_validate_safe_branch_name_accepts_valid_names():
    from app.services.git_service import validate_safe_branch_name

    for value in ("staging", "master", "release/staging"):
        validate_safe_branch_name(value, "branch")  # khong raise


def test_validate_safe_branch_name_rejects_unsafe_names():
    import pytest

    from app.services.git_service import GitError, validate_safe_branch_name

    for value in (
        "--upload-pack=x",
        "-x",
        "a..b",
        "a//b",
        "",
        "a/",
        "a.lock",
    ):
        with pytest.raises(GitError):
            validate_safe_branch_name(value, "branch")
