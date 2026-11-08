"""Execute the reusable contributor workflow against real local Git histories."""
import os
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "contributor-check.yml"


def git(repo, *args, email="owner@example.test"):
    env = os.environ.copy()
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="Fixture Owner",
        GIT_COMMITTER_NAME="Fixture Owner",
        GIT_AUTHOR_EMAIL=email,
        GIT_COMMITTER_EMAIL=email,
    )
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def commit(repo, filename, email):
    (repo / filename).write_text(filename + "\n")
    git(repo, "add", filename)
    git(repo, "commit", "-m", filename, email=email)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def history(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--template=", "--initial-branch=main")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "release.py").write_text("AUTHOR_MAP = {}\n")
    (repo / "contributors" / "emails").mkdir(parents=True)
    (repo / "contributors" / "emails" / "owner@example.test").write_text("owner\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "shared base")
    common = git(repo, "rev-parse", "HEAD")
    commit(repo, "unrelated-main", "main-history@example.test")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "checkout", "-b", "maintenance/served", common)
    base = commit(repo, "already-in-maintenance", "old-maintenance@example.test")
    commit(repo, "actual-pr-change", "owner@example.test")
    return repo, base


def check(repo, base, event="pull_request"):
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    step = next(
        item for item in workflow["jobs"]["check-attribution"]["steps"]
        if item.get("id") == "check-emails"
    )
    env = os.environ.copy()
    env.update(
        GITHUB_EVENT_NAME=event,
        PR_BASE_SHA=base,
        GITHUB_OUTPUT=str(repo / "step-output"),
    )
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=repo, env=env, capture_output=True, text=True, timeout=20,
    )


def test_maintenance_pr_checks_only_commits_after_its_event_base(history):
    repo, base = history
    result = check(repo, base)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "old-maintenance@example.test" not in result.stdout
    assert "main-history@example.test" not in result.stdout


def test_event_base_stays_pinned_when_branch_advances(history):
    repo, base = history
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-b", "advanced-maintenance", base)
    advanced = commit(repo, "later-base-work", "later-history@example.test")
    git(repo, "update-ref", "refs/remotes/origin/maintenance/served", advanced)
    git(repo, "checkout", "--detach", head)
    result = check(repo, base)
    assert result.returncode == 0, result.stdout + result.stderr


def test_actual_new_unmapped_contributor_still_fails(history):
    repo, base = history
    commit(repo, "unmapped-pr-author", "new-unmapped@example.test")
    result = check(repo, base)
    assert result.returncode != 0
    assert "new-unmapped@example.test" in result.stdout


@pytest.mark.parametrize("base", ["", "not-a-commit", "--all", "$(touch injected)"])
def test_invalid_pr_base_fails_closed_without_shell_evaluation(history, base):
    repo, _ = history
    result = check(repo, base)
    assert result.returncode != 0
    assert not (repo / "injected").exists()


def test_postmerge_push_retains_existing_default_branch_behavior(history):
    repo, _ = history
    git(repo, "checkout", "--detach", "origin/main")
    result = check(repo, "", event="push")
    assert result.returncode == 0, result.stdout + result.stderr
