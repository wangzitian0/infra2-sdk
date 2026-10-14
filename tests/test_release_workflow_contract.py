"""Workflow and documentation contract tests for release identity (#18, #70, #75)."""

from __future__ import annotations

import copy
import os
import subprocess
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "release.yml"
CONTRIBUTING_PATH = ROOT / "CONTRIBUTING.md"


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _find_step_by_script(steps: list[dict[str, Any]], needle: str) -> dict[str, Any] | None:
    for step in steps:
        run_cmd = step.get("run")
        if isinstance(run_cmd, str) and needle in run_cmd:
            return step
    return None


def validate_workflow_contracts(
    ci_data: dict[str, Any],
    release_data: dict[str, Any],
) -> list[str]:
    """Validate the four workflow guards across ci.yml and release.yml and return violations."""
    violations: list[str] = []

    ci_jobs = ci_data.get("jobs") or {}
    if not ci_jobs:
        violations.append("ci.yml: no jobs defined")
    for job_name, job in ci_jobs.items():
        if job.get("timeout-minutes") != 15:
            violations.append(f"ci.yml:{job_name}: timeout-minutes must equal 15")

    release_jobs = release_data.get("jobs") or {}
    if not release_jobs:
        violations.append("release.yml: no jobs defined")
    for job_name, job in release_jobs.items():
        if job.get("timeout-minutes") != 15:
            violations.append(f"release.yml:{job_name}: timeout-minutes must equal 15")

    test_job = ci_jobs.get("test") or {}
    test_steps = test_job.get("steps") or []
    version_step = _find_step_by_script(test_steps, "src/infra2_sdk/")
    if version_step is None:
        violations.append("ci.yml:test: missing version-bump gate step for src/infra2_sdk/")
    else:
        if version_step.get("if") != "github.event_name == 'pull_request'":
            violations.append("ci.yml:test: version-bump gate must run on pull_request")
        script = str(version_step.get("run", ""))
        for required_token in (
            "git fetch origin main",
            "git fetch origin --tags --force",
            "git diff --name-only origin/main...HEAD -- src/infra2_sdk/",
            "origin/main:pyproject.toml",
            "refs/tags/v${head_version}",
        ):
            if required_token not in script:
                violations.append(f"ci.yml:test: version-bump gate missing {required_token!r}")

    rel_job = release_jobs.get("release") or {}
    if rel_job.get("environment") != "production":
        violations.append("release.yml:release: environment must equal 'production'")

    rel_steps = rel_job.get("steps") or []
    ancestor_step = _find_step_by_script(rel_steps, "merge-base --is-ancestor")
    publish_step = _find_step_by_script(rel_steps, "gh release create")
    if publish_step is None:
        violations.append("release.yml:release: missing gh release create step")
    if ancestor_step is None:
        violations.append("release.yml:release: missing origin/main ancestor verification step")
    else:
        rel_script = str(ancestor_step.get("run", ""))
        if "git fetch origin main" not in rel_script:
            violations.append("release.yml:release: ancestor step must fetch origin/main")
        if 'git merge-base --is-ancestor "$GITHUB_SHA" origin/main' not in rel_script:
            violations.append(
                "release.yml:release: ancestor step must check "
                'git merge-base --is-ancestor "$GITHUB_SHA" origin/main'
            )
        if publish_step is not None:
            ancestor_idx = next(i for i, s in enumerate(rel_steps) if s is ancestor_step)
            publish_idx = next(i for i, s in enumerate(rel_steps) if s is publish_step)
            if ancestor_idx > publish_idx:
                violations.append(
                    "release.yml:release: ancestor step must execute before gh release create"
                )

    return violations


def test_committed_workflows_satisfy_all_four_guards() -> None:
    ci_data = _load_yaml(CI_WORKFLOW_PATH)
    release_data = _load_yaml(RELEASE_WORKFLOW_PATH)
    assert validate_workflow_contracts(ci_data, release_data) == []


def test_mutation_removing_timeout_minutes_fails_guard() -> None:
    ci_data = _load_yaml(CI_WORKFLOW_PATH)
    release_data = _load_yaml(RELEASE_WORKFLOW_PATH)

    for job_name in ("test", "package", "extras-smoke"):
        mutated_ci = copy.deepcopy(ci_data)
        mutated_ci["jobs"][job_name].pop("timeout-minutes", None)
        assert f"ci.yml:{job_name}: timeout-minutes must equal 15" in validate_workflow_contracts(
            mutated_ci, release_data
        )

    mutated_rel = copy.deepcopy(release_data)
    mutated_rel["jobs"]["release"]["timeout-minutes"] = 60
    assert "release.yml:release: timeout-minutes must equal 15" in validate_workflow_contracts(
        ci_data, mutated_rel
    )


def test_mutation_removing_ci_version_bump_gate_fails_guard() -> None:
    ci_data = _load_yaml(CI_WORKFLOW_PATH)
    release_data = _load_yaml(RELEASE_WORKFLOW_PATH)

    mutated_ci = copy.deepcopy(ci_data)
    mutated_ci["jobs"]["test"]["steps"] = [
        s
        for s in mutated_ci["jobs"]["test"]["steps"]
        if "src/infra2_sdk/" not in str(s.get("run", ""))
    ]
    assert (
        "ci.yml:test: missing version-bump gate step for src/infra2_sdk/"
        in validate_workflow_contracts(mutated_ci, release_data)
    )

    mutated_tag = copy.deepcopy(ci_data)
    step = _find_step_by_script(mutated_tag["jobs"]["test"]["steps"], "src/infra2_sdk/")
    assert step is not None
    step["run"] = str(step["run"]).replace("refs/tags/v${head_version}", "refs/heads/main")
    assert any(
        "refs/tags/v${head_version}" in v
        for v in validate_workflow_contracts(mutated_tag, release_data)
    )


def test_mutation_removing_release_ancestor_check_or_production_env_fails_guard() -> None:
    ci_data = _load_yaml(CI_WORKFLOW_PATH)
    release_data = _load_yaml(RELEASE_WORKFLOW_PATH)

    mutated_env = copy.deepcopy(release_data)
    mutated_env["jobs"]["release"].pop("environment", None)
    assert (
        "release.yml:release: environment must equal 'production'"
        in validate_workflow_contracts(ci_data, mutated_env)
    )

    mutated_anc = copy.deepcopy(release_data)
    mutated_anc["jobs"]["release"]["steps"] = [
        s
        for s in mutated_anc["jobs"]["release"]["steps"]
        if "merge-base --is-ancestor" not in str(s.get("run", ""))
    ]
    assert (
        "release.yml:release: missing origin/main ancestor verification step"
        in validate_workflow_contracts(ci_data, mutated_anc)
    )

    mutated_pub = copy.deepcopy(release_data)
    mutated_pub["jobs"]["release"]["steps"] = [
        s
        for s in mutated_pub["jobs"]["release"]["steps"]
        if "gh release create" not in str(s.get("run", ""))
    ]
    assert "release.yml:release: missing gh release create step" in validate_workflow_contracts(
        ci_data, mutated_pub
    )


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _init_origin_and_clone(tmp_path: Path, initial_version: str = "3.5.0") -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "main")

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "remote", "add", "origin", str(origin))
    (work / "pyproject.toml").write_text(
        f'[project]\nname = "infra2-sdk"\nversion = "{initial_version}"\n',
        encoding="utf-8",
    )
    pkg = work / "src" / "infra2_sdk"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    (work / "README.md").write_text("# readme\n", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "initial")
    _git(work, "tag", f"v{initial_version}")
    _git(work, "push", "-u", "origin", "main", "--tags")
    return origin, work


def test_ci_version_bump_gate_script_behavior_on_git_repo(tmp_path: Path) -> None:
    ci_data = _load_yaml(CI_WORKFLOW_PATH)
    step = _find_step_by_script(ci_data["jobs"]["test"]["steps"], "src/infra2_sdk/")
    assert step is not None
    script = str(step["run"])

    _origin, work = _init_origin_and_clone(tmp_path, initial_version="3.5.0")

    # Case 1: PR changes only README.md -> passes without version bump
    _git(work, "checkout", "-b", "pr-docs-only")
    (work / "README.md").write_text("# updated readme\n", encoding="utf-8")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "docs change")
    res_docs = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=work,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_docs.returncode == 0, res_docs.stderr

    # Case 2: PR changes src/infra2_sdk/ without bumping pyproject.toml -> fails
    (work / "src" / "infra2_sdk" / "__init__.py").write_text("x = 2\n", encoding="utf-8")
    _git(work, "add", "src/infra2_sdk/__init__.py")
    _git(work, "commit", "-m", "code change without bump")
    res_no_bump = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=work,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_no_bump.returncode == 1
    assert "equals origin/main" in res_no_bump.stderr

    # Case 3: PR bumps pyproject.toml to a version whose tag already exists on origin -> fails
    _git(work, "tag", "v3.5.1")
    _git(work, "push", "origin", "v3.5.1")
    (work / "pyproject.toml").write_text(
        '[project]\nname = "infra2-sdk"\nversion = "3.5.1"\n',
        encoding="utf-8",
    )
    _git(work, "add", "pyproject.toml")
    _git(work, "commit", "-m", "bump to existing tag")
    res_existing_tag = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=work,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_existing_tag.returncode == 1
    assert "v3.5.1 already exists" in res_existing_tag.stderr

    # Case 4: PR bumps pyproject.toml to a fresh version -> passes
    (work / "pyproject.toml").write_text(
        '[project]\nname = "infra2-sdk"\nversion = "3.5.2"\n',
        encoding="utf-8",
    )
    _git(work, "add", "pyproject.toml")
    _git(work, "commit", "-m", "bump to fresh tag")
    res_fresh = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=work,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_fresh.returncode == 0, res_fresh.stderr


def test_release_ancestor_gate_script_behavior_on_git_repo(tmp_path: Path) -> None:
    release_data = _load_yaml(RELEASE_WORKFLOW_PATH)
    step = _find_step_by_script(
        release_data["jobs"]["release"]["steps"], "merge-base --is-ancestor"
    )
    assert step is not None
    script = str(step["run"])

    _origin, work = _init_origin_and_clone(tmp_path, initial_version="3.5.0")
    main_sha = _git(work, "rev-parse", "HEAD")

    # Simulate tag-triggered detached checkout where refs/remotes/origin/main is absent initially
    _git(work, "checkout", "--detach", "v3.5.0")
    _git(work, "update-ref", "-d", "refs/remotes/origin/main")

    # Commit on origin/main passes ancestor check even when refs/remotes/origin/main was absent
    res_on_main = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=work,
        env={**os.environ, "GITHUB_SHA": main_sha},
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_on_main.returncode == 0, res_on_main.stderr

    # Unmerged side-branch commit fails ancestor check
    _git(work, "checkout", "-b", "unmerged-release")
    (work / "README.md").write_text("# unmerged\n", encoding="utf-8")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "unmerged commit")
    branch_sha = _git(work, "rev-parse", "HEAD")

    res_off_main = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=work,
        env={**os.environ, "GITHUB_SHA": branch_sha},
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_off_main.returncode != 0


def test_contributing_documents_release_guards_and_usage_census() -> None:
    text = CONTRIBUTING_PATH.read_text(encoding="utf-8")
    assert "environment: production" in text
    assert 'git merge-base --is-ancestor "$GITHUB_SHA" origin/main' in text
    assert "tools.sdk_usage_census" in text
    assert "src/infra2_sdk/" in text
