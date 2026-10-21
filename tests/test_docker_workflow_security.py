"""Keep GHCR write permission off PR and session-branch Docker checks."""

from __future__ import annotations

import pathlib

import pytest
import yaml

WORKFLOW = (
    pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "docker-image.yml"
)
PUBLISH_IF = (
    "github.event_name == 'push' && "
    "(github.ref == 'refs/heads/main' || startsWith(github.ref, 'refs/tags/'))"
)
BUILD_ONLY_IF = (
    "github.event_name != 'push' || "
    "(github.ref != 'refs/heads/main' && !startsWith(github.ref, 'refs/tags/'))"
)


@pytest.fixture(scope="module")
def spec() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_ghcr_push_triggers_are_main_session_ci_and_version_tags(spec: dict) -> None:
    triggers = spec.get("on", spec.get(True))  # PyYAML parses `on` as True.
    assert triggers["push"]["branches"] == ["main", "arena/**"]
    assert triggers["push"]["tags"] == ["v*"]
    assert triggers["pull_request"]["branches"] == ["main"]
    assert "pull_request_target" not in triggers


def test_fork_pr_and_session_build_job_never_get_registry_write(spec: dict) -> None:
    triggers = spec.get("on", spec.get(True))
    assert "pull_request" in triggers
    assert "workflow_dispatch" in triggers
    assert spec["permissions"] == {"contents": "read"}

    build_job = spec["jobs"]["build-smoke-publish"]
    assert build_job["if"] == BUILD_ONLY_IF
    assert build_job.get("permissions", {}) == {}
    assert all("secrets." not in str(step) for step in build_job["steps"])


def test_only_main_or_version_tag_pushes_can_log_in_or_publish(spec: dict) -> None:
    publish_job = spec["jobs"]["publish"]
    assert publish_job["if"] == PUBLISH_IF
    assert publish_job["permissions"] == {"contents": "read", "packages": "write"}

    steps = publish_job["steps"]
    build = next(
        step for step in steps if step.get("uses") == "./.github/actions/docker-build-smoke"
    )
    login = next(step for step in steps if step.get("name") == "Log in to GHCR")
    publish = next(step for step in steps if step.get("name") == "Publish to GHCR")
    assert steps.index(build) < steps.index(login) < steps.index(publish)
    assert login["with"]["password"] == "${{ secrets.GITHUB_TOKEN }}"


def test_main_and_release_tags_keep_expected_aliases(spec: dict) -> None:
    steps = spec["jobs"]["publish"]["steps"]
    publish = next(step for step in steps if step.get("name") == "Publish to GHCR")
    script = publish["run"]
    assert "for tag in latest edge" in script
    assert 'tag="${GITHUB_REF#refs/tags/}"' in script
    assert 'docker push "$IMAGE:$tag"' in script
