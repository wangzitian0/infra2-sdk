"""Workflow and documentation contract tests for release identity (#18, #70, #75)."""

from __future__ import annotations

import copy
import os
import subprocess
import sys
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
    """Validate the job timeouts and the release ancestor guard; return the violations."""
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

    rel_job = release_jobs.get("release") or {}
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


def test_committed_workflows_satisfy_the_guards() -> None:
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


def test_mutation_removing_release_ancestor_check_or_publish_fails_guard() -> None:
    ci_data = _load_yaml(CI_WORKFLOW_PATH)
    release_data = _load_yaml(RELEASE_WORKFLOW_PATH)

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


def test_release_tag_version_gate_script_behavior(tmp_path: Path) -> None:
    release_data = _load_yaml(RELEASE_WORKFLOW_PATH)
    steps = release_data["jobs"]["release"]["steps"]
    step = _find_step_by_script(steps, "GITHUB_REF_NAME")
    publish_step = _find_step_by_script(steps, "gh release create")
    assert step is not None, "release.yml: missing the tag-equals-version step"
    assert publish_step is not None
    assert steps.index(step) < steps.index(publish_step)
    script = str(step["run"])

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "infra2-sdk"\nversion = "3.5.0"\n', encoding="utf-8"
    )
    path = f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}"

    def run(ref_name: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-euo", "pipefail", "-c", script],
            cwd=tmp_path,
            env={**os.environ, "PATH": path, "GITHUB_REF_NAME": ref_name},
            capture_output=True,
            text=True,
            check=False,
        )

    matching = run("v3.5.0")
    assert matching.returncode == 0, matching.stderr
    assert run("v3.5.1").returncode != 0
    assert run("3.5.0").returncode != 0


def test_contributing_documents_release_guards_and_usage_census() -> None:
    text = CONTRIBUTING_PATH.read_text(encoding="utf-8")
    assert 'git merge-base --is-ancestor "$GITHUB_SHA" origin/main' in text
    assert "tools.sdk_usage_census" in text
