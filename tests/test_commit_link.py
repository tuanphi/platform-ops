"""Link commit GitLab trong ket qua tat/bat replicas va tin Telegram."""

import pytest

from app.services import env_toggle_service, git_service
from tests.test_git_service import _replicas_config

SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


@pytest.mark.parametrize(
    "repo_url, expected",
    [
        ("https://gitlab.g-pay.vn/devops/charts.git", f"https://gitlab.g-pay.vn/devops/charts/-/commit/{SHA}"),
        ("https://user:tok@gitlab.g-pay.vn/devops/charts", f"https://gitlab.g-pay.vn/devops/charts/-/commit/{SHA}"),
        ("ssh://git@gitlab.g-pay.vn:10022/development/devops/charts.git",
         f"https://gitlab.g-pay.vn/development/devops/charts/-/commit/{SHA}"),
        ("git@gitlab.g-pay.vn:devops/charts.git", ""),
        ("", ""),
    ],
)
def test_commit_web_url(repo_url, expected):
    assert git_service.commit_web_url(repo_url, SHA) == expected


def test_toggle_returns_commit_url_after_push(monkeypatch, tmp_path):
    rel_path = "envValues/values-sandbox.yaml"
    yaml_path = tmp_path / rel_path
    yaml_path.parent.mkdir(parents=True)
    yaml_path.write_text('core:\n  api:\n    replicas: "0"\n')
    monkeypatch.setattr(git_service, "ensure_staging_repo", lambda repo_url: str(tmp_path))

    def fake_run(args, cwd):
        if args[:2] == ["git", "status"]:
            return rel_path
        if args[:2] == ["git", "rev-parse"]:
            return SHA + "\n"
        return ""
    monkeypatch.setattr(git_service, "_run", fake_run)

    result = git_service.toggle_staging_app("core", "api", 1, "u@example.com", _replicas_config(rel_path))
    assert result.commit_url == f"https://example.com/repo/-/commit/{SHA}"


def test_telegram_has_commit_line_under_result(monkeypatch):
    sent = []
    monkeypatch.setattr(env_toggle_service.notify_service, "send_infra_toggle_telegram", lambda text, env: sent.append(text))
    result = git_service.ReplicasToggleResult(
        ok=True, message="Đã cập nhật", changed=(("core", "api"),), commit_url=f"https://example.com/repo/-/commit/{SHA}"
    )
    env_toggle_service.notify("Turn on staging: core/api (replicas=1)", "u@example.com", result, "staging")
    lines = sent[0].split("\n")
    assert lines[2].startswith("<b>- Result:</b>")
    assert lines[3] == f'<b>- Commit:</b> <a href="https://example.com/repo/-/commit/{SHA}">a1b2c3d4</a>'
    assert lines[4].startswith("<b>- Applications:</b>")


def test_merge_request_url_from_push_output_or_fallback():
    out = "\nremote:\nremote: View merge request for production-replicas-1:\nremote:   https://gitlab.g-pay.vn/devops/charts/-/merge_requests/42\nremote:\n"
    assert git_service.merge_request_web_url(out, "https://gitlab.g-pay.vn/devops/charts.git", "b") == \
        "https://gitlab.g-pay.vn/devops/charts/-/merge_requests/42"
    assert git_service.merge_request_web_url("", "ssh://git@gitlab.g-pay.vn:10022/devops/charts.git", "production-replicas-1") == \
        "https://gitlab.g-pay.vn/devops/charts/-/merge_requests?scope=all&state=all&source_branch=production-replicas-1"


def test_telegram_shows_merge_request_line_in_mr_mode(monkeypatch):
    sent = []
    monkeypatch.setattr(env_toggle_service.notify_service, "send_infra_toggle_telegram", lambda text, env: sent.append(text))
    result = git_service.ReplicasToggleResult(
        ok=True, message="Đã tạo Merge Request vào nhánh master", changed=(("core", "api"),), pending_merge_request=True,
        commit_url=f"https://example.com/repo/-/commit/{SHA}", merge_request_url="https://example.com/repo/-/merge_requests/42",
    )
    env_toggle_service.notify("Turn on production: core/api (replicas=1)", "u@example.com", result, "production")
    lines = sent[0].split("\n")
    assert lines[2] == "<b>- Result:</b> Merge Request created to branch master"
    assert lines[3] == '<b>- Merge Request:</b> <a href="https://example.com/repo/-/merge_requests/42">!42</a>'
    assert "Commit:" not in sent[0]
