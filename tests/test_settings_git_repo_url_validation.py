"""Test regression/moi cho ban va bao mat validate_safe_repo_url (Settings > Git,
section=git) - xem app/services/git_service.py::validate_safe_repo_url +
app/routers/settings_router.py::update_general nhanh section=="git".

Chi test theo yeu cau [TEST] tu Leader (TC1-TC4) - monkeypatch
app.services.git_service.remote_branch_exists de tranh goi git that ra mang
(router import cuc bo ham nay ben trong update_general, phai monkeypatch dung
module goc app.services.git_service, khop convention cua
tests/test_settings_git_branch_fields.py)."""

import pytest

from app.models import Role
from app.services.app_setting_service import get_app_setting
from app.services.git_service import GitError, validate_safe_repo_url
from tests.conftest import make_user

VALID_PAYLOAD = {
    "section": "git",
    "git_remote_repo_url": "https://gitlab.g-pay.vn/devops/helm/applications.git",
    "git_commit_author_email": "form-deploy@g-pay.vn",
    "git_commit_author_name": "form-deploy",
    "git_branch_staging": "staging",
    "git_branch_master": "master",
    "staging_values_file_path": "envValues/values-sandbox.yaml",
    "production_values_file_path": "envValues/values-production.yaml",
}


# ---------------------------------------------------------------------------
# TC1: regression happy path - repo URL https hop le + branch hop le, mock
# remote_branch_exists=True -> ok=True, DB duoc cap nhat.
# ---------------------------------------------------------------------------


def test_update_git_repo_url_success_with_valid_https_url(client_factory, db_session, monkeypatch):
    import app.services.git_service as git_service

    monkeypatch.setattr(git_service, "remote_branch_exists", lambda repo_url, branch: True)

    sa = make_user(db_session, email="sa@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)

    resp = client.post(
        "/settings/general",
        data=VALID_PAYLOAD,
        follow_redirects=False,
        headers={"accept": "application/json"},
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    updated = get_app_setting(db_session)
    assert updated.git_remote_repo_url == VALID_PAYLOAD["git_remote_repo_url"]


# ---------------------------------------------------------------------------
# TC2: fix MEDIUM - repo URL bat dau bang "-" (CWE-88 option injection) hoac
# scheme nguy hiem (file://, ext::) phai bi chan TRUOC khi goi remote_branch_exists,
# DB khong doi.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_url",
    [
        "--upload-pack=touch /tmp/pwned",
        "-x",
        "file:///etc/passwd",
        "ext::sh -c id",
    ],
)
def test_update_git_repo_url_rejected_when_dangerous(client_factory, db_session, monkeypatch, bad_url):
    import app.services.git_service as git_service

    called = []
    monkeypatch.setattr(
        git_service, "remote_branch_exists", lambda repo_url, branch: called.append((repo_url, branch)) or True
    )

    sa = make_user(db_session, email="sa@example.com", role=Role.SUPER_ADMIN)
    client = client_factory(user=sa)
    original_url = get_app_setting(db_session).git_remote_repo_url

    resp = client.post(
        "/settings/general",
        data={**VALID_PAYLOAD, "git_remote_repo_url": bad_url},
        follow_redirects=False,
        headers={"accept": "application/json"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert not called, f"khong duoc goi remote_branch_exists khi repo URL '{bad_url}' khong hop le"
    assert get_app_setting(db_session).git_remote_repo_url == original_url


# ---------------------------------------------------------------------------
# TC3 (unit): validate_safe_repo_url - chap nhan https:// va ssh://, tu choi
# cac bien the injection/scheme nguy hiem/newline, va tu choi http:// (bi loai
# khoi whitelist moi).
# ---------------------------------------------------------------------------


def test_validate_safe_repo_url_accepts_https_and_ssh():
    validate_safe_repo_url("https://gitlab.example.com/devops/helm/applications.git", "repo_url")
    validate_safe_repo_url("ssh://git@gitlab.example.com/devops/helm/applications.git", "repo_url")


@pytest.mark.parametrize(
    "bad_url",
    [
        "-x",
        "--upload-pack=x",
        "file:///etc/passwd",
        "ext::sh -c x",
        "https://gitlab.example.com/repo.git\n",
        "https://gitlab.example.com/repo.git\r",
        "http://gitlab.example.com/repo.git",
        "",
    ],
)
def test_validate_safe_repo_url_rejects_unsafe_values(bad_url):
    with pytest.raises(GitError):
        validate_safe_repo_url(bad_url, "repo_url")


# ---------------------------------------------------------------------------
# TC4: fix LOW - validate_safe_branch_name tu choi ten nhanh chua "\n"/"\r" o
# cuoi (vd "master\n"), van chap nhan ten nhanh hop le.
# ---------------------------------------------------------------------------


def test_validate_safe_branch_name_rejects_trailing_newline_or_carriage_return():
    from app.services.git_service import validate_safe_branch_name

    for value in ("master\n", "master\r"):
        with pytest.raises(GitError):
            validate_safe_branch_name(value, "branch")


def test_validate_safe_branch_name_still_accepts_valid_names():
    from app.services.git_service import validate_safe_branch_name

    for value in ("staging", "master", "release/staging"):
        validate_safe_branch_name(value, "branch")  # khong raise
