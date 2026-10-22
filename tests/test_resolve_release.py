from __future__ import annotations

from copy import deepcopy

import pytest

from scripts.resolve_release import ReleaseError, resolve_release

SHA = "a" * 40
REPOSITORY = "ebianchini/tech-challenge-fase-3"


class GitHubFixture:
    def __init__(self):
        self.run = {
            "id": 123,
            "head_sha": SHA,
            "head_branch": "main",
            "head_repository": {"full_name": REPOSITORY},
            "status": "completed",
            "conclusion": "success",
        }
        self.status = "identical"
        self.gate = "success"
        self.tag_sha = SHA
        self.older_run = None

    def get(self, path):
        if path.startswith("compare/"):
            return {"status": self.status}
        if path.startswith("actions/workflows/"):
            return {"workflow_runs": [self.run] + ([self.older_run] if self.older_run else [])}
        if path.startswith("actions/runs/"):
            return {"jobs": [{"name": "CI required", "conclusion": self.gate}]}
        if path.startswith("git/ref/"):
            return {"object": {"type": "tag", "sha": "b" * 40}}
        if path.startswith("git/tags/"):
            return {"object": {"type": "commit", "sha": self.tag_sha}}
        raise AssertionError(path)


def dispatch(api, environment="staging", sha=SHA, tag=""):
    return resolve_release(
        "workflow_dispatch",
        {"inputs": {"commit_sha": sha, "environment": environment, "release_tag": tag}},
        REPOSITORY, "refs/heads/main", api,
    )


def test_release_uses_exact_successful_commit_and_annotated_tag():
    result = dispatch(GitHubFixture(), "production", tag="v1.2.3")
    assert result == {"sha": SHA, "environment": "production", "ci_run_id": "123"}


@pytest.mark.parametrize("field,value", [("status", "diverged"), ("gate", "skipped")])
def test_release_rejects_unmerged_commits_and_skipped_gate(field, value):
    api = GitHubFixture()
    setattr(api, field, value)
    with pytest.raises(ReleaseError):
        dispatch(api)


def test_failed_newer_ci_cannot_reuse_an_old_success():
    api = GitHubFixture()
    api.older_run = deepcopy(api.run)
    api.older_run["id"] = 122
    api.run["conclusion"] = "failure"
    with pytest.raises(ReleaseError, match="mais recente"):
        dispatch(api)


@pytest.mark.parametrize("tag", ["", "latest", "v1.0.0;echo bad"])
def test_production_requires_release_tag(tag):
    with pytest.raises(ReleaseError, match="tag de release"):
        dispatch(GitHubFixture(), "production", tag=tag)


def test_production_tag_cannot_point_to_another_commit():
    api = GitHubFixture()
    api.tag_sha = "c" * 40
    with pytest.raises(ReleaseError, match="commit aprovado"):
        dispatch(api, "production", tag="v1.0.0")


@pytest.mark.parametrize("sha", ["main", SHA + "\n", "$(echo bad)", "a" * 7])
def test_release_requires_literal_full_commit(sha):
    with pytest.raises(ReleaseError, match="SHA completo"):
        dispatch(GitHubFixture(), sha=sha)


def test_fork_and_pull_request_cannot_trigger_privileged_cd():
    api = GitHubFixture()
    run = api.run | {"event": "push"}
    for overrides in (
        {"event": "pull_request"},
        {"head_repository": {"full_name": "someone/fork"}},
        {"head_branch": "feature"},
    ):
        with pytest.raises(ReleaseError, match="main deste repositorio"):
            resolve_release(
                "workflow_run", {"workflow_run": run | overrides},
                REPOSITORY, "refs/heads/main", api,
            )
    assert resolve_release(
        "workflow_run", {"workflow_run": run}, REPOSITORY, "refs/heads/main", api
    )["environment"] == "dev"
