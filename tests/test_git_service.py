"""Kiểm thử app/services/git_service.py - mock subprocess.run hoàn toàn, KHÔNG gọi git
CLI/network thật."""

from types import SimpleNamespace

import pytest

from app.services import git_service


def _fake_run(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_check_commit_on_staging_rejects_short_commitid(monkeypatch, db_session):
    result = git_service.check_commit_on_staging("core", "api", "abc12", "https://example.com/repo.git")
    assert result.status == "not_found"
    assert "định dạng" in result.message


def test_check_commit_on_staging_rejects_non_hex_commitid(monkeypatch):
    """Fix bao mat: commitid chua ky tu khong phai hex (vd metachar regex nhu ".*") phai
    bi tu choi truoc khi dua vao `git log --grep=...` - tranh khop bua vao commit khac."""
    calls = []
    monkeypatch.setattr(git_service, "ensure_staging_repo", lambda repo_url: "/tmp/fake")
    monkeypatch.setattr(git_service, "_run", lambda args, cwd: (calls.append(args), "")[1])

    result = git_service.check_commit_on_staging("core", "api", ".*rebase", "https://example.com/repo.git")

    assert result.status == "not_found"
    assert "định dạng" in result.message
    assert calls == []  # khong duoc goi git log voi pattern chua duoc validate


def test_check_commit_on_staging_not_found_when_project_absent(monkeypatch):
    monkeypatch.setattr(git_service, "ensure_staging_repo", lambda repo_url: "/tmp/fake")
    monkeypatch.setattr(git_service, "_run", lambda args, cwd: "some unrelated log output\n")

    result = git_service.check_commit_on_staging("core", "api", "abc1234", "https://example.com/repo.git")

    assert result.status == "not_found"


def test_check_commit_on_staging_project_mismatch(monkeypatch):
    monkeypatch.setattr(git_service, "ensure_staging_repo", lambda repo_url: "/tmp/fake")
    # Format that ky vong boi COMMIT_MESSAGE_RE: "<author>/<project>/<application>:
    # Update image version to <version>" (commit do pipeline CI ngoai tao tren nhanh
    # staging, khac format perform_run dung khi push master).
    commit_log = "ci-bot/core/other-app: Update image version to abc1234-staging\n"
    monkeypatch.setattr(git_service, "_run", lambda args, cwd: commit_log)

    result = git_service.check_commit_on_staging("core", "api", "abc1234", "https://example.com/repo.git")

    assert result.status == "project_mismatch"


def test_check_commit_on_staging_ok_when_matched(monkeypatch):
    monkeypatch.setattr(git_service, "ensure_staging_repo", lambda repo_url: "/tmp/fake")
    commit_log = "ci-bot/core/api: Update image version to abc1234-staging\n"
    monkeypatch.setattr(git_service, "_run", lambda args, cwd: commit_log)

    result = git_service.check_commit_on_staging("core", "api", "abc1234", "https://example.com/repo.git")

    assert result.status == "ok"
    assert result.image_version == "abc1234-staging"


def test_ensure_repo_raises_when_repo_url_missing():
    with pytest.raises(git_service.GitError, match="Repo URL"):
        git_service._ensure_repo("/tmp/whatever", "master", "")


def test_run_raises_git_error_and_redacts_credentials_on_failure(monkeypatch):
    def fake_subprocess_run(args, **kwargs):
        return _fake_run(
            returncode=1,
            stderr="fatal: could not read from 'https://user:s3cr3t-token@gitlab.example.com/repo.git'",
        )

    monkeypatch.setattr(git_service.subprocess, "run", fake_subprocess_run)

    with pytest.raises(git_service.GitError) as exc_info:
        git_service._run(["git", "fetch"], cwd="/tmp/fake")

    message = str(exc_info.value)
    assert "s3cr3t-token" not in message
    assert "***@" in message


def test_apply_version_and_push_reports_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(git_service, "ensure_master_repo", lambda repo_url: str(tmp_path))
    config = git_service.ChartPushConfig(
        chart_values_file_template="charts/{project}/{application}/values-production.yaml",
        git_commit_author_email="bot@example.com",
        git_commit_author_name="bot",
        git_remote_repo_url="https://example.com/repo.git",
    )

    result = git_service._apply_version_and_push("core", "api", "v2", "msg", config)

    assert result.ok is False
    assert "không tồn tại" in result.message


def test_apply_version_and_push_reports_yaml_structure_error(monkeypatch, tmp_path):
    """FIXED (A2): khi update_image_version raise YamlStructureError (file YAML thiếu key
    project/application/version), _apply_version_and_push phải trả ok=False và KHÔNG
    được commit/push gì cả - không được để lỗi lan ra ngoài thành exception chưa bắt."""
    rel_path = "charts/core/api/values-production.yaml"
    yaml_path = tmp_path / rel_path
    yaml_path.parent.mkdir(parents=True)
    yaml_path.write_text('other-group:\n  other-app:\n    version: "v1"\n')

    monkeypatch.setattr(git_service, "ensure_master_repo", lambda repo_url: str(tmp_path))

    calls = []
    monkeypatch.setattr(git_service, "_run", lambda args, cwd: (calls.append(args) or ""))

    config = git_service.ChartPushConfig(
        chart_values_file_template="charts/{project}/{application}/values-production.yaml",
        git_commit_author_email="bot@example.com",
        git_commit_author_name="bot",
        git_remote_repo_url="https://example.com/repo.git",
    )

    result = git_service._apply_version_and_push("core", "api", "v2", "msg", config)

    assert result.ok is False
    assert "sai cấu trúc" in result.message
    assert calls == []  # không gọi git status/commit/push khi cấu trúc YAML sai


def test_apply_version_and_push_stops_when_file_unchanged(monkeypatch, tmp_path):
    """Nếu update_image_version không thực sự đổi nội dung file (vd version mới ==
    version cũ), `git status` sẽ không liệt kê file đó -> phải dừng lại, KHÔNG commit/push
    (tránh commit rỗng hoặc che giấu lỗi ghi sai field)."""
    rel_path = "charts/core/api/values-production.yaml"
    yaml_path = tmp_path / rel_path
    yaml_path.parent.mkdir(parents=True)
    yaml_path.write_text('core:\n  api:\n    version: "v1"\n')

    monkeypatch.setattr(git_service, "ensure_master_repo", lambda repo_url: str(tmp_path))
    monkeypatch.setattr("app.services.yaml_service.update_image_version", lambda *a, **k: None)
    monkeypatch.setattr(git_service, "_run", lambda args, cwd: "" if args[:2] == ["git", "status"] else "")

    config = git_service.ChartPushConfig(
        chart_values_file_template="charts/{project}/{application}/values-production.yaml",
        git_commit_author_email="bot@example.com",
        git_commit_author_name="bot",
        git_remote_repo_url="https://example.com/repo.git",
    )

    result = git_service._apply_version_and_push("core", "api", "v1", "msg", config)

    assert result.ok is False
    assert "unchanged" in result.message


# ---------------------------------------------------------------------------
# Bat/Tat staging + Production replicas (toggle_staging_app/toggle_production_app,
# bulk_toggle_*, turn_off_all_staging) - dung chung 1 file YAML nhieu app.
# ---------------------------------------------------------------------------


def _replicas_config(
    rel_path="envValues/values-sandbox.yaml",
    charts_dir="charts",
    excluded_dirs="",
    use_merge_request=False,
    target_branch="",
):
    return git_service.EnvReplicasPushConfig(
        values_file_path=rel_path,
        catalog_charts_dir=charts_dir,
        catalog_excluded_dirs=excluded_dirs,
        git_commit_author_email="bot@example.com",
        git_commit_author_name="bot",
        git_remote_repo_url="https://example.com/repo.git",
        use_merge_request=use_merge_request,
        target_branch=target_branch,
    )


def _make_chart_app(tmp_path, group, application, charts_dir="charts"):
    app_dir = tmp_path / charts_dir / group / application
    app_dir.mkdir(parents=True)
    (app_dir / "Chart.yaml").write_text("name: " + application + "\n")


def test_toggle_staging_app_happy_path_commits_and_pushes(monkeypatch, tmp_path):
    rel_path = "envValues/values-sandbox.yaml"
    yaml_path = tmp_path / rel_path
    yaml_path.parent.mkdir(parents=True)
    yaml_path.write_text('core:\n  api:\n    replicas: "0"\n')

    monkeypatch.setattr(git_service, "ensure_staging_repo", lambda repo_url: str(tmp_path))
    calls = []
    monkeypatch.setattr(
        git_service,
        "_run",
        lambda args, cwd: (calls.append(args), (rel_path if args[:2] == ["git", "status"] else ""))[1],
    )

    result = git_service.toggle_staging_app("core", "api", 3, "user@example.com", _replicas_config(rel_path))

    assert result.ok is True
    assert result.changed == (("core", "api"),)
    assert ["git", "push"] in calls
    assert 'replicas: "3"' in yaml_path.read_text()


def test_toggle_staging_app_not_found_in_file(monkeypatch, tmp_path):
    rel_path = "envValues/values-sandbox.yaml"
    yaml_path = tmp_path / rel_path
    yaml_path.parent.mkdir(parents=True)
    yaml_path.write_text('other:\n  other-app:\n    replicas: "0"\n')

    monkeypatch.setattr(git_service, "ensure_staging_repo", lambda repo_url: str(tmp_path))
    calls = []
    monkeypatch.setattr(git_service, "_run", lambda args, cwd: calls.append(args))

    result = git_service.toggle_staging_app("core", "api", 1, "user@example.com", _replicas_config(rel_path))

    assert result.ok is False
    assert "Không tìm thấy" in result.message
    assert calls == []  # khong goi git status/commit/push khi khong tim thay app


def test_bulk_toggle_staging_apps_sets_replicas_1_or_0(monkeypatch, tmp_path):
    rel_path = "envValues/values-sandbox.yaml"
    yaml_path = tmp_path / rel_path
    yaml_path.parent.mkdir(parents=True)
    yaml_path.write_text('core:\n  api:\n    replicas: "0"\n  worker:\n    replicas: "0"\n')

    monkeypatch.setattr(git_service, "ensure_staging_repo", lambda repo_url: str(tmp_path))
    monkeypatch.setattr(
        git_service, "_run", lambda args, cwd: rel_path if args[:2] == ["git", "status"] else ""
    )

    result = git_service.bulk_toggle_staging_apps(
        [("core", "api"), ("core", "worker")], True, "user@example.com", _replicas_config(rel_path)
    )

    assert result.ok is True
    assert set(result.changed) == {("core", "api"), ("core", "worker")}
    assert 'api:\n    replicas: "1"' in yaml_path.read_text()


def test_turn_off_all_staging_sets_every_app_to_zero(monkeypatch, tmp_path):
    rel_path = "envValues/values-sandbox.yaml"
    yaml_path = tmp_path / rel_path
    yaml_path.parent.mkdir(parents=True)
    yaml_path.write_text('core:\n  api:\n    replicas: "3"\n  worker:\n    replicas: "1"\n')

    monkeypatch.setattr(git_service, "ensure_staging_repo", lambda repo_url: str(tmp_path))
    monkeypatch.setattr(
        git_service, "_run", lambda args, cwd: rel_path if args[:2] == ["git", "status"] else ""
    )

    result = git_service.turn_off_all_staging("user@example.com", _replicas_config(rel_path))

    assert result.ok is True
    assert set(result.changed) == {("core", "api"), ("core", "worker")}
    data = yaml_path.read_text()
    assert 'replicas: "0"' in data
    assert '"3"' not in data and '"1"' not in data


def test_toggle_production_app_uses_master_checkout(monkeypatch, tmp_path):
    """Production phai dung ensure_master_repo (nhanh master), KHONG phai
    ensure_staging_repo - xac nhan dung checkout dung yeu cau."""
    rel_path = "envValues/values-production.yaml"
    yaml_path = tmp_path / rel_path
    yaml_path.parent.mkdir(parents=True)
    yaml_path.write_text('core:\n  api:\n    replicas: "1"\n')

    monkeypatch.setattr(git_service, "ensure_master_repo", lambda repo_url: str(tmp_path))

    def _fail_staging(repo_url):
        raise AssertionError("khong duoc goi ensure_staging_repo cho production")

    monkeypatch.setattr(git_service, "ensure_staging_repo", _fail_staging)
    monkeypatch.setattr(
        git_service, "_run", lambda args, cwd: rel_path if args[:2] == ["git", "status"] else ""
    )

    result = git_service.toggle_production_app("core", "api", 5, "user@example.com", _replicas_config(rel_path))

    assert result.ok is True
    assert 'replicas: "5"' in yaml_path.read_text()


# ---------------------------------------------------------------------------
# list_staging_apps / list_production_apps - danh sach app QUYET DINH boi thu muc
# charts/{group}/{application}/ (GIONG HET catalog_service.sync_catalog_from_chart_repo),
# replicas doc tu file values TREN CUNG checkout - khong qua catalog DB.
# ---------------------------------------------------------------------------


def test_list_staging_apps_reads_chart_dirs_and_matches_replicas_from_file(monkeypatch, tmp_path):
    _make_chart_app(tmp_path, "core", "api")
    _make_chart_app(tmp_path, "core", "worker")
    _make_chart_app(tmp_path, "other", "app")

    rel_path = "envValues/values-sandbox.yaml"
    yaml_path = tmp_path / rel_path
    yaml_path.parent.mkdir(parents=True)
    yaml_path.write_text('core:\n  api:\n    replicas: "3"\n  worker:\n    replicas: "0"\n')

    monkeypatch.setattr(git_service, "ensure_staging_repo", lambda repo_url: str(tmp_path))

    result = git_service.list_staging_apps(_replicas_config(rel_path))

    # "other/app" co trong thu muc chart nhung CHUA co entry trong file values -> mac dinh "0"
    assert set(result) == {("core", "api", "3"), ("core", "worker", "0"), ("other", "app", "0")}


def test_list_staging_apps_respects_excluded_dirs(monkeypatch, tmp_path):
    _make_chart_app(tmp_path, "core", "api")
    _make_chart_app(tmp_path, "core", "redis")

    monkeypatch.setattr(git_service, "ensure_staging_repo", lambda repo_url: str(tmp_path))

    result = git_service.list_staging_apps(_replicas_config(excluded_dirs="redis,rabbitmq"))

    assert result == [("core", "api", "0")]


# ---------------------------------------------------------------------------
# Production Merge Request mode (config.use_merge_request=True) - thay vi push thang,
# push len 1 nhanh tam + GitLab push options (-o merge_request.create) de tao Merge
# Request. Staging KHONG co che do nay (use_merge_request luon False).
# ---------------------------------------------------------------------------


def test_toggle_production_app_merge_request_mode_pushes_with_options_not_direct(monkeypatch, tmp_path):
    rel_path = "envValues/values-production.yaml"
    yaml_path = tmp_path / rel_path
    yaml_path.parent.mkdir(parents=True)
    yaml_path.write_text('core:\n  api:\n    replicas: "1"\n')

    monkeypatch.setattr(git_service, "ensure_master_repo", lambda repo_url: str(tmp_path))
    calls = []

    def _fake_run(args, cwd):
        calls.append(args)
        if args[:2] == ["git", "status"]:
            return rel_path
        return ""

    monkeypatch.setattr(git_service, "_run", _fake_run)

    config = _replicas_config(rel_path, use_merge_request=True, target_branch="master")
    result = git_service.toggle_production_app("core", "api", 5, "user@example.com", config)

    assert result.ok is True
    assert result.pending_merge_request is True
    assert 'replicas: "5"' in yaml_path.read_text()

    assert ["git", "push"] not in calls  # khong duoc push thang

    push_calls = [c for c in calls if c[:2] == ["git", "push"]]
    assert len(push_calls) == 1
    push_args = push_calls[0]
    assert "-o" in push_args
    assert "merge_request.create" in push_args
    assert "merge_request.target=master" in push_args
    assert any(a.startswith("HEAD:refs/heads/production-replicas-") for a in push_args)
    assert "origin" in push_args


def test_bulk_toggle_production_apps_merge_request_mode(monkeypatch, tmp_path):
    rel_path = "envValues/values-production.yaml"
    yaml_path = tmp_path / rel_path
    yaml_path.parent.mkdir(parents=True)
    yaml_path.write_text('core:\n  api:\n    replicas: "0"\n  worker:\n    replicas: "0"\n')

    monkeypatch.setattr(git_service, "ensure_master_repo", lambda repo_url: str(tmp_path))
    monkeypatch.setattr(
        git_service, "_run", lambda args, cwd: rel_path if args[:2] == ["git", "status"] else ""
    )

    config = _replicas_config(rel_path, use_merge_request=True, target_branch="master")
    result = git_service.bulk_toggle_production_apps(
        [("core", "api"), ("core", "worker")], True, "user@example.com", config
    )

    assert result.ok is True
    assert result.pending_merge_request is True
    assert set(result.changed) == {("core", "api"), ("core", "worker")}


def test_toggle_production_app_direct_push_mode_has_no_pending_merge_request(monkeypatch, tmp_path):
    """use_merge_request=False (mac dinh) phai di duong push thang nhu cu -
    pending_merge_request phai la False."""
    rel_path = "envValues/values-production.yaml"
    yaml_path = tmp_path / rel_path
    yaml_path.parent.mkdir(parents=True)
    yaml_path.write_text('core:\n  api:\n    replicas: "1"\n')

    monkeypatch.setattr(git_service, "ensure_master_repo", lambda repo_url: str(tmp_path))
    calls = []
    monkeypatch.setattr(
        git_service,
        "_run",
        lambda args, cwd: (calls.append(args), (rel_path if args[:2] == ["git", "status"] else ""))[1],
    )

    result = git_service.toggle_production_app("core", "api", 5, "user@example.com", _replicas_config(rel_path))

    assert result.ok is True
    assert result.pending_merge_request is False
    assert ["git", "push"] in calls


def test_merge_request_mode_rejects_invalid_target_branch(monkeypatch, tmp_path):
    rel_path = "envValues/values-production.yaml"
    yaml_path = tmp_path / rel_path
    yaml_path.parent.mkdir(parents=True)
    yaml_path.write_text('core:\n  api:\n    replicas: "1"\n')

    monkeypatch.setattr(git_service, "ensure_master_repo", lambda repo_url: str(tmp_path))

    config = _replicas_config(rel_path, use_merge_request=True, target_branch="--upload-pack=/bin/sh")

    with pytest.raises(git_service.GitError):
        git_service.toggle_production_app("core", "api", 5, "user@example.com", config)


def test_list_staging_apps_returns_empty_when_charts_dir_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(git_service, "ensure_staging_repo", lambda repo_url: str(tmp_path))

    result = git_service.list_staging_apps(_replicas_config())

    assert result == []


def test_list_production_apps_uses_master_checkout(monkeypatch, tmp_path):
    """Production phai quet chart dir + doc replicas tren checkout MASTER, KHONG phai
    staging - xac nhan dung nhanh theo yeu cau."""
    _make_chart_app(tmp_path, "core", "api")
    rel_path = "envValues/values-production.yaml"
    yaml_path = tmp_path / rel_path
    yaml_path.parent.mkdir(parents=True)
    yaml_path.write_text('core:\n  api:\n    replicas: "2"\n')

    monkeypatch.setattr(git_service, "ensure_master_repo", lambda repo_url: str(tmp_path))

    def _fail_staging(repo_url):
        raise AssertionError("khong duoc goi ensure_staging_repo cho production")

    monkeypatch.setattr(git_service, "ensure_staging_repo", _fail_staging)

    result = git_service.list_production_apps(_replicas_config(rel_path))

    assert result == [("core", "api", "2")]
