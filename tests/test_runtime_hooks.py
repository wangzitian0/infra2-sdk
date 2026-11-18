"""Falsifiable unit tests for runtime verification hooks and next_patch_tag (#170)."""

from __future__ import annotations

import json
import subprocess
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

import infra2_sdk.manifests as _manifests_mod
from infra2_sdk import (
    CELL_POLICIES,
    HOOK_SLOTS,
    POLICY_VERSION,
    CellOverrides,
    CheckRow,
    HookRunResult,
    HookSlotSpec,
    OverrideReason,
    RuntimeHooksManifest,
    format_check_grid,
    load_hooks_manifest,
    next_patch_tag,
    resolve_check_cells,
    run_cell,
    run_hook_slot,
)
from infra2_sdk.manifests import (
    CURRENT_HOOKS_SCHEMA_VERSION,
    FORBIDDEN_HOOK_ENV_NAMES,
    FORBIDDEN_HOOK_ENV_PREFIXES,
    SLOT_MAX_TIMEOUT_SECONDS,
    STAGE_HOOK_PLANS,
    CheckSpec,
    StageHookPlan,
    _derive_views,
    _load_policy_table,
    build_hook_env,
    main,
    stage_hook_plan,
)
from infra2_sdk.observability import CheckVerdict, load_trail


@pytest.fixture(autouse=True)
def _run_in_tmp_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # run_hook_slot writes <cwd>/.artifacts/hooks/checks.jsonl


def _sample_v2_manifest_dict(*, service: str = "finance_report/app") -> dict[str, object]:
    return {
        "schema_version": 2,
        "service": service,
        "slots": {
            "smoke": {
                "run": "python -m scripts.smoke",
                "timeout_seconds": 60,
            },
            "preview_gate": {
                "run": "pytest -m e2e_preview",
                "bootstrap": "playwright install chromium",
                "timeout_seconds": 300,
                "required_env": ["E2E_USER_EMAIL"],
                "optional_env": ["E2E_LOCALE"],
                "artifacts_dir": ".artifacts/preview",
            },
            "staging_gate": {
                "run": "pytest -m staging_canary",
                "timeout_seconds": 420,
                "required_env": ["LLM_API_KEY"],
            },
            "prod_gate": {
                "run": "python -m scripts.prod_surface",
                "timeout_seconds": 180,
            },
            "scheduled_audit": {
                "run": "python -m scripts.audit_replay",
                "timeout_seconds": 1800,
            },
        },
    }


def test_v2_manifest_stage_hook_plans_and_slot_ceilings(tmp_path: Path) -> None:
    assert CURRENT_HOOKS_SCHEMA_VERSION == 3
    assert POLICY_VERSION == 1
    assert not hasattr(_manifests_mod, "LEGACY_V1_HOOK_SLOTS")
    assert HOOK_SLOTS == ("smoke", "preview_gate", "staging_gate", "prod_gate", "scheduled_audit")
    assert SLOT_MAX_TIMEOUT_SECONDS == {
        "smoke": 120,
        "preview_gate": 360,
        "staging_gate": 600,
        "prod_gate": 300,
        "scheduled_audit": 3600,
    }
    assert stage_hook_plan("production") == STAGE_HOOK_PLANS["prod"]
    with pytest.raises(ValueError, match="unknown stage"):
        stage_hook_plan("canary")
    with pytest.raises(ValueError, match="stage must be a string"):
        stage_hook_plan(123)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        SLOT_MAX_TIMEOUT_SECONDS["smoke"] = 999  # type: ignore[index]
    with pytest.raises(TypeError):
        STAGE_HOOK_PLANS["prod"] = StageHookPlan("prod", (), None, None, stage_id="x")  # type: ignore[index]

    v2 = RuntimeHooksManifest.from_dict(_sample_v2_manifest_dict())
    assert v2.schema_version == 2
    assert tuple(v2.slots) == HOOK_SLOTS
    with pytest.raises(TypeError):
        v2.slots["smoke"] = v2.slots["prod_gate"]  # type: ignore[index]
    assert RuntimeHooksManifest.from_dict(v2.to_dict()) == v2

    # v2 rejects bootstrap on inline smoke
    bad_smoke = _sample_v2_manifest_dict()
    bad_smoke["slots"] = {  # type: ignore[index]
        **bad_smoke["slots"],  # type: ignore[dict-item]
        "smoke": {"run": "echo ok", "bootstrap": "npm ci", "timeout_seconds": 60},
    }
    with pytest.raises(ValueError, match="bootstrap"):
        RuntimeHooksManifest.from_dict(bad_smoke)

    # v2 enforces per-slot timeout ceiling
    for slot, ceiling in SLOT_MAX_TIMEOUT_SECONDS.items():
        if ceiling >= 3600:
            continue
        over = _sample_v2_manifest_dict()
        over["slots"] = {  # type: ignore[index]
            **over["slots"],  # type: ignore[dict-item]
            slot: {"run": "echo ok", "timeout_seconds": ceiling + 1},
        }
        with pytest.raises(ValueError, match="exceeds"):
            RuntimeHooksManifest.from_dict(over)

    # Schema 1 support is completely removed: loading or checking v1 fails closed.
    v1_path = tmp_path / "v1.manifest.json"
    v1_raw = _sample_v2_manifest_dict()
    v1_raw["schema_version"] = 1
    v1_path.write_text(json.dumps(v1_raw, indent=2), encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version 2 or 3"):
        load_hooks_manifest("v1.manifest.json", root=tmp_path)

    out_strict: list[str] = []
    assert main(["--check-hooks", "v1.manifest.json"], root=tmp_path, echo=out_strict.append) == 1
    assert any("schema_version 2 or 3" in line for line in out_strict)


def test_runtime_hooks_manifest_roundtrip_and_load(tmp_path: Path) -> None:
    raw = _sample_v2_manifest_dict()
    manifest_file = tmp_path / "runtime-hooks.manifest.json"
    manifest_file.write_text(json.dumps(raw, indent=2), encoding="utf-8")

    loaded = load_hooks_manifest("runtime-hooks.manifest.json", root=tmp_path)
    assert loaded.schema_version == 2
    assert loaded.service == "finance_report/app"
    assert tuple(loaded.slots) == HOOK_SLOTS
    assert loaded.slots["preview_gate"].bootstrap == "playwright install chromium"
    assert loaded.slots["preview_gate"].required_env == ("E2E_USER_EMAIL",)
    assert loaded.slots["preview_gate"].optional_env == ("E2E_LOCALE",)
    assert loaded.slots["preview_gate"].artifacts_dir == ".artifacts/preview"
    assert loaded.slots["smoke"].artifacts_dir == ".artifacts/hooks"

    roundtripped = RuntimeHooksManifest.from_dict(loaded.to_dict())
    assert roundtripped == loaded


@pytest.mark.parametrize(
    "bad_secret",
    [
        *sorted(FORBIDDEN_HOOK_ENV_NAMES),
        *(f"{prefix}SECRET" for prefix in FORBIDDEN_HOOK_ENV_PREFIXES),
    ],
)
def test_hook_slot_rejects_forbidden_platform_secrets(bad_secret: str) -> None:
    with pytest.raises(ValueError, match="forbidden platform secret"):
        HookSlotSpec(run="echo ok", required_env=(bad_secret,))
    with pytest.raises(ValueError, match="forbidden platform secret"):
        HookSlotSpec(run="echo ok", optional_env=(bad_secret,))


@pytest.mark.parametrize(
    "bad_env_name",
    ["", "lowercase", "1LEADING_DIGIT", "BAD-HYPHEN", "SPACE IN NAME"],
)
def test_hook_slot_rejects_invalid_env_names(bad_env_name: str) -> None:
    with pytest.raises(ValueError, match="must match"):
        HookSlotSpec(run="echo ok", required_env=(bad_env_name,))


def test_hook_slot_and_manifest_validation_errors() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        HookSlotSpec(run="   ")
    with pytest.raises(ValueError, match="timeout_seconds"):
        HookSlotSpec(run="echo ok", timeout_seconds=0)
    with pytest.raises(ValueError, match="timeout_seconds"):
        HookSlotSpec(run="echo ok", timeout_seconds=3601)
    with pytest.raises(ValueError, match="timeout_seconds"):
        HookSlotSpec(run="echo ok", timeout_seconds=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="duplicate"):
        HookSlotSpec(run="echo ok", required_env=("FOO", "FOO"))
    with pytest.raises(ValueError, match="both required_env and optional_env"):
        HookSlotSpec(run="echo ok", required_env=("FOO",), optional_env=("FOO",))
    with pytest.raises(ValueError, match="unknown hook slot field"):
        HookSlotSpec.from_dict({"run": "echo ok", "extra": 1})

    base = _sample_v2_manifest_dict()
    with pytest.raises(ValueError, match="schema_version 2 or 3"):
        RuntimeHooksManifest.from_dict({**base, "schema_version": 1})
    with pytest.raises(ValueError, match="schema_version 2 or 3"):
        RuntimeHooksManifest.from_dict({**base, "schema_version": 4})
    with pytest.raises(ValueError, match="schema_version 2 or 3"):
        RuntimeHooksManifest.from_dict({**base, "schema_version": True})
    with pytest.raises(ValueError, match="unknown runtime hooks manifest field"):
        RuntimeHooksManifest.from_dict({**base, "unexpected": "x"})

    missing_slot = dict(base["slots"])  # type: ignore[arg-type]
    missing_slot.pop("scheduled_audit")
    with pytest.raises(ValueError, match="missing required slot"):
        RuntimeHooksManifest.from_dict({**base, "slots": missing_slot})

    extra_slot = {**base["slots"], "custom_slot": {"run": "echo ok"}}  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="unknown hook slot"):
        RuntimeHooksManifest.from_dict({**base, "slots": extra_slot})


def test_hook_slot_checkout_lineage() -> None:
    default_spec = HookSlotSpec(run="echo ok")
    assert default_spec.checkout_lineage is False
    assert "checkout_lineage" not in default_spec.to_dict()
    assert HookSlotSpec.from_dict(default_spec.to_dict()) == default_spec

    explicit_spec = HookSlotSpec(run="echo ok", checkout_lineage=True)
    assert explicit_spec.checkout_lineage is True
    assert explicit_spec.to_dict()["checkout_lineage"] is True
    roundtripped = HookSlotSpec.from_dict(explicit_spec.to_dict())
    assert roundtripped == explicit_spec
    assert roundtripped.checkout_lineage is True

    with pytest.raises(ValueError, match="hook slot 'checkout_lineage' must be a bool"):
        HookSlotSpec(run="echo ok", checkout_lineage=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="hook slot 'checkout_lineage' must be a bool"):
        HookSlotSpec(run="echo ok", checkout_lineage="true")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="hook slot 'checkout_lineage' must be a bool"):
        HookSlotSpec.from_dict({"run": "echo ok", "checkout_lineage": 1})
    with pytest.raises(ValueError, match="hook slot 'checkout_lineage' must be a bool"):
        HookSlotSpec.from_dict({"run": "echo ok", "checkout_lineage": "true"})


def test_build_hook_env_strips_ambient_secrets_and_injects_canonical_vars() -> None:
    spec = HookSlotSpec(
        run="pytest",
        required_env=("APP_API_TOKEN",),
        optional_env=("OPTIONAL_FLAG", "EMPTY_OPTIONAL"),
        artifacts_dir=".artifacts/custom",
    )
    ambient = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/Users/tester",
        "INFRA2_PAT": "ghp_super_secret",
        "VAULT_TOKEN": "s.vault_secret",
        "AWS_SECRET_ACCESS_KEY": "aws_secret",
        "UNDECLARED_VAR": "should_be_stripped",
        "APP_API_TOKEN": "token-123",
        "OPTIONAL_FLAG": "enabled",
        "EMPTY_OPTIONAL": "   ",
    }
    clean = build_hook_env(
        spec,
        slot="preview_gate",
        app_url="https://pr-42.preview.example.com",
        environment="preview",
        version_ref="abc1234",
        artifacts_dir=spec.artifacts_dir,
        ambient_env=ambient,
    )
    assert clean["PATH"] == "/usr/bin:/bin"
    assert clean["HOME"] == "/Users/tester"
    assert clean["APP_API_TOKEN"] == "token-123"
    assert clean["OPTIONAL_FLAG"] == "enabled"
    assert "EMPTY_OPTIONAL" not in clean
    assert "INFRA2_PAT" not in clean
    assert "VAULT_TOKEN" not in clean
    assert "AWS_SECRET_ACCESS_KEY" not in clean
    assert "UNDECLARED_VAR" not in clean
    assert clean["APP_URL"] == "https://pr-42.preview.example.com"
    assert clean["DEPLOY_BASE_URL"] == "https://pr-42.preview.example.com"
    assert clean["STAGING_BASE_URL"] == "https://pr-42.preview.example.com"
    assert clean["TA_BASE_URL"] == "https://pr-42.preview.example.com"
    assert clean["ENVIRONMENT"] == "preview"
    assert clean["APP_VERSION_REF"] == "abc1234"
    assert clean["EXPECTED_SHA"] == "abc1234"
    assert clean["HOOK_SLOT"] == "preview_gate"
    assert clean["HOOK_ARTIFACTS_DIR"] == ".artifacts/custom"


def test_build_hook_env_defaults_pythonpath_to_dot_and_allows_explicit_override() -> None:
    default_spec = HookSlotSpec(run="pytest")
    clean_default = build_hook_env(
        default_spec,
        slot="smoke",
        app_url="http://127.0.0.1:8000",
        environment="local_dev",
        version_ref="local",
        artifacts_dir=default_spec.artifacts_dir,
        ambient_env={"PATH": "/usr/bin", "PYTHONPATH": "/ambient/ignored"},
    )
    assert clean_default["PYTHONPATH"] == "."

    override_opt = HookSlotSpec(run="pytest", optional_env=("PYTHONPATH",))
    clean_opt = build_hook_env(
        override_opt,
        slot="smoke",
        app_url="http://127.0.0.1:8000",
        environment="local_dev",
        version_ref="local",
        artifacts_dir=override_opt.artifacts_dir,
        ambient_env={"PYTHONPATH": "src:apps/web"},
    )
    assert clean_opt["PYTHONPATH"] == "src:apps/web"

    override_req = HookSlotSpec(run="pytest", required_env=("PYTHONPATH",))
    clean_req = build_hook_env(
        override_req,
        slot="smoke",
        app_url="http://127.0.0.1:8000",
        environment="local_dev",
        version_ref="local",
        artifacts_dir=override_req.artifacts_dir,
        ambient_env={"PYTHONPATH": "custom/lib"},
    )
    assert clean_req["PYTHONPATH"] == "custom/lib"


def test_run_hook_slot_missing_required_env_fails_closed(tmp_path: Path) -> None:
    manifest = RuntimeHooksManifest.from_dict(_sample_v2_manifest_dict())
    result_file = tmp_path / "out" / "hook-result.json"
    calls: list[list[str]] = []

    def fake_runner(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    res = run_hook_slot(
        manifest,
        "preview_gate",
        app_url="https://preview.example.com",
        environment="preview",
        version_ref="1234567",
        ambient_env={"PATH": "/usr/bin"},
        result_path=result_file,
        runner=fake_runner,
    )
    assert calls == []
    assert res.status == "fail"
    assert res.exit_code == 2
    assert res.failed_step == "env"
    assert "E2E_USER_EMAIL" in res.summary
    saved = json.loads(result_file.read_text(encoding="utf-8"))
    assert saved["status"] == "fail"
    assert saved["failed_step"] == "env"


def test_run_hook_slot_bootstrap_failure_skips_run_command() -> None:
    manifest = RuntimeHooksManifest.from_dict(_sample_v2_manifest_dict())
    calls: list[list[str]] = []

    def fake_runner(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 17, stdout="", stderr="playwright install failed")

    res = run_hook_slot(
        manifest,
        "preview_gate",
        app_url="https://preview.example.com",
        environment="preview",
        version_ref="1234567",
        ambient_env={"E2E_USER_EMAIL": "bot@example.com"},
        runner=fake_runner,
    )
    assert len(calls) == 1
    assert calls[0] == ["sh", "-lc", "playwright install chromium"]
    assert res.status == "fail"
    assert res.exit_code == 17
    assert res.failed_step == "bootstrap"
    assert "playwright install failed" in res.stderr_tail


def test_run_hook_slot_timeout_records_timeout_step() -> None:
    manifest = RuntimeHooksManifest.from_dict(_sample_v2_manifest_dict())

    def timeout_runner(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=60, stderr="hung on request")

    res = run_hook_slot(
        manifest,
        "smoke",
        app_url="https://staging.example.com",
        environment="staging",
        version_ref="v0.1.0",
        ambient_env={},
        runner=timeout_runner,
    )
    assert res.status == "fail"
    assert res.exit_code == 124
    assert res.failed_step == "timeout"
    assert "timed out after 60s" in res.summary
    assert "hung on request" in res.stderr_tail


def test_run_hook_slot_preserves_both_stdout_and_stderr_when_both_non_empty() -> None:
    manifest = RuntimeHooksManifest.from_dict(_sample_v2_manifest_dict())

    def dual_stream_runner(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout="Error: ERROR: Playwright does not support chromium on ubuntu26.04-x64\n",
            stderr="Installed 16 packages in 9ms\n",
        )

    res = run_hook_slot(
        manifest,
        "preview_gate",
        app_url="https://preview.example.com",
        environment="preview",
        version_ref="1234567",
        ambient_env={"E2E_USER_EMAIL": "bot@example.com"},
        runner=dual_stream_runner,
    )
    assert res.status == "fail"
    assert res.failed_step == "bootstrap"
    assert "--- stdout ---" in res.stderr_tail
    assert (
        "Error: ERROR: Playwright does not support chromium on ubuntu26.04-x64" in res.stderr_tail
    )
    assert "--- stderr ---" in res.stderr_tail
    assert "Installed 16 packages in 9ms" in res.stderr_tail

    def huge_dual_stream_runner(
        cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout=("A" * 2500) + "STDOUT_FINAL_MARKER",
            stderr=("B" * 2500) + "STDERR_FINAL_MARKER",
        )

    huge_res = run_hook_slot(
        manifest,
        "smoke",
        app_url="https://preview.example.com",
        environment="preview",
        version_ref="1234567",
        ambient_env={},
        runner=huge_dual_stream_runner,
    )
    assert len(huge_res.stderr_tail) <= 2000
    assert "--- stdout ---" in huge_res.stderr_tail
    assert "STDOUT_FINAL_MARKER" in huge_res.stderr_tail
    assert "--- stderr ---" in huge_res.stderr_tail
    assert "STDERR_FINAL_MARKER" in huge_res.stderr_tail

    def dual_timeout_runner(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(
            cmd=cmd,
            timeout=60,
            output="stdout before timeout",
            stderr="stderr before timeout",
        )

    timeout_res = run_hook_slot(
        manifest,
        "smoke",
        app_url="https://staging.example.com",
        environment="staging",
        version_ref="v0.1.0",
        ambient_env={},
        runner=dual_timeout_runner,
    )
    assert timeout_res.failed_step == "timeout"
    assert "--- stdout ---" in timeout_res.stderr_tail
    assert "stdout before timeout" in timeout_res.stderr_tail
    assert "--- stderr ---" in timeout_res.stderr_tail
    assert "stderr before timeout" in timeout_res.stderr_tail


def test_cli_check_hooks_and_run_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest_path = tmp_path / "runtime-hooks.manifest.json"
    raw = _sample_v2_manifest_dict(service="truealpha/app")
    raw["slots"]["smoke"] = {  # type: ignore[index]
        "run": 'test "$APP_URL" = "http://127.0.0.1:8000" && test -z "$INFRA2_PAT"',
        "timeout_seconds": 30,
    }
    manifest_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")

    output: list[str] = []
    rc = main(["--check-hooks", "runtime-hooks.manifest.json"], root=tmp_path, echo=output.append)
    assert rc == 0
    assert output == ["runtime hooks manifest: ok (truealpha/app, v2, 5 slots)"]

    monkeypatch.setenv("INFRA2_PAT", "must-not-leak-into-hook")
    result_rel = ".artifacts/smoke-result.json"
    run_out: list[str] = []
    rc_run = main(
        [
            "--run-hook",
            "smoke",
            "--hooks-manifest",
            "runtime-hooks.manifest.json",
            "--app-url",
            "http://127.0.0.1:8000",
            "--env",
            "preview",
            "--version-ref",
            "deadbeef",
            "--result-file",
            result_rel,
        ],
        root=tmp_path,
        echo=run_out.append,
    )
    assert rc_run == 0
    payload = json.loads(run_out[-1])
    assert payload["status"] == "pass"
    assert payload["service"] == "truealpha/app"
    assert payload["slot"] == "smoke"
    assert payload["app_url"] == "http://127.0.0.1:8000"
    assert payload["version_ref"] == "deadbeef"
    assert (tmp_path / result_rel).is_file()


def test_hook_run_result_validation() -> None:
    with pytest.raises(ValueError, match="status"):
        HookRunResult(
            service="s",
            slot="smoke",
            environment="preview",
            app_url="http://localhost",
            version_ref="v1",
            status="unknown",
            exit_code=1,
            duration_seconds=0.1,
        )
    with pytest.raises(ValueError, match="failed_step"):
        HookRunResult(
            service="s",
            slot="smoke",
            environment="preview",
            app_url="http://localhost",
            version_ref="v1",
            status="fail",
            exit_code=1,
            duration_seconds=0.1,
            failed_step="invalid",
        )


def test_next_patch_tag_computes_highest_semver_and_handles_empty() -> None:
    assert next_patch_tag([]) == "v0.1.0"
    assert next_patch_tag(["main", "sha-1234567", "v1.2.3-rc1"]) == "v0.1.0"
    assert next_patch_tag([], initial="v1.0.0") == "v1.0.0"
    assert (
        next_patch_tag(["v0.1.0", "refs/tags/v0.1.9", "v0.2.0", "v0.1.12", "not-a-tag"]) == "v0.2.1"
    )
    assert next_patch_tag(["v1.4.9", "v1.4.10", "v0.99.99"]) == "v1.4.11"
    with pytest.raises(ValueError, match="initial must match"):
        next_patch_tag([], initial="1.0.0")


def test_stage_hook_plan_phases() -> None:
    release = ("contract", "provenance", "env", "upstream_post")
    plan = stage_hook_plan("staging")
    assert (plan.operation_slot, plan.post_slot, plan.pre_checks) == (
        "smoke",
        "staging_gate",
        release,
    )
    assert stage_hook_plan("prod").pre_checks == release
    assert stage_hook_plan("preview") == StageHookPlan(
        "preview", ("contract",), "smoke", "preview_gate", stage_id="preview.runtime"
    )
    assert stage_hook_plan("schedule") == StageHookPlan(
        "schedule", (), None, "scheduled_audit", stage_id="staging.provider_regression"
    )
    assert not hasattr(plan, "deploy_slot")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert plan.operation_slot == "smoke"


def test_hook_slot_checks_and_run_are_exclusive() -> None:
    check = CheckSpec("a", ("true",))
    with pytest.raises(ValueError, match="not both"):
        HookSlotSpec(run="echo ok", checks=(check,))
    with pytest.raises(ValueError, match="not both"):
        HookSlotSpec()
    with pytest.raises(ValueError, match="duplicate"):
        HookSlotSpec(checks=(check, check))
    with pytest.raises(ValueError, match="argv"):
        CheckSpec("a", ())
    spec = HookSlotSpec.from_dict({"checks": [{"id": "a", "argv": ["true"]}]})
    assert spec.checks == (check,)
    assert HookSlotSpec.from_dict(spec.to_dict()) == spec


def _checks_manifest(**slot: object) -> RuntimeHooksManifest:
    raw = _sample_v2_manifest_dict()
    raw["slots"] = {**raw["slots"], "smoke": {"timeout_seconds": 60, **slot}}  # type: ignore[dict-item]
    return RuntimeHooksManifest.from_dict(raw)


def test_run_hook_slot_runs_each_check_and_records_it(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[0] == "b":
            return subprocess.CompletedProcess(cmd, 3, "", "\nboom first\nsecond")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    kw = {"app_url": "https://x.example.com", "cwd": tmp_path, "ambient_env": {}, "runner": fake}
    file = tmp_path / ".artifacts/hooks/checks.jsonl"
    file.parent.mkdir(parents=True)
    file.write_text('{"source": "stale", "checks": [], "complete": true}\n')

    m = _checks_manifest(
        checks=[
            {"id": "a", "argv": ["a"]},
            {"id": "b", "argv": ["b"]},
            {"id": "c", "argv": ["c"]},
        ]
    )
    res = run_hook_slot(m, "smoke", **kw)  # type: ignore[arg-type]
    assert [c[0] for c in calls] == ["a", "b"]
    assert (res.status, res.exit_code, res.failed_step) == ("fail", 3, "run")
    assert [(c.name, c.ok, c.detail) for c in res.checks] == [
        ("a", True, ""),
        ("b", False, "boom first"),
    ]
    assert all(c.seconds >= 0 for c in res.checks) and isinstance(res.checks[0], CheckVerdict)
    assert len(file.read_text().splitlines()) == 1  # stale line deleted, one line written
    trail = load_trail(file, expected_sources=["smoke"])
    assert trail.is_green("a") and "b" in trail.failing

    calls.clear()
    res = run_hook_slot(_checks_manifest(run="echo ok"), "smoke", **kw)  # type: ignore[arg-type]
    assert res.status == "pass" and calls == [["sh", "-lc", "echo ok"]]
    assert [(c.name, c.ok) for c in res.checks] == [("smoke", True)]
    assert load_trail(file, expected_sources=["smoke"]).is_green("smoke")
    assert "a" not in load_trail(file, expected_sources=["smoke"]).green


@pytest.mark.parametrize("exc", [FileNotFoundError, KeyboardInterrupt])
def test_run_hook_slot_raising_runner_still_writes_the_check_file(
    tmp_path: Path, exc: type[BaseException]
) -> None:
    def boom(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "b":
            raise exc("no such binary: b")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    m = _checks_manifest(checks=[{"id": "a", "argv": ["a"]}, {"id": "b", "argv": ["b"]}])
    with pytest.raises(exc):
        run_hook_slot(
            m, "smoke", app_url="https://x.example.com", cwd=tmp_path, ambient_env={}, runner=boom
        )
    trail = load_trail(tmp_path / ".artifacts/hooks/checks.jsonl", expected_sources=["smoke"])
    assert trail.is_green("a")
    assert "no such binary" in trail.failing["b"].detail


def test_checks_of_one_slot_share_one_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    timeouts: list[float] = []

    def slow(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        timeouts.append(kw["timeout"])  # type: ignore[arg-type]
        now[0] += 0.4  # each check needs more than a third of the 1 s slot budget
        if kw["timeout"] < 0.4:  # type: ignore[operator]
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])  # type: ignore[arg-type]
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(
        "infra2_sdk.manifests.time", SimpleNamespace(monotonic=lambda: now[0]), raising=True
    )
    m = _checks_manifest(
        timeout_seconds=1, checks=[{"id": i, "argv": [i]} for i in ("a", "b", "c")]
    )
    res = run_hook_slot(m, "smoke", app_url="https://x.example.com", ambient_env={}, runner=slow)
    assert timeouts == pytest.approx([1.0, 0.6, 0.2])
    assert (res.status, res.exit_code, res.failed_step) == ("fail", 124, "timeout")
    assert [(c.name, c.ok) for c in res.checks] == [("a", True), ("b", True), ("c", False)]


def test_run_hook_slot_detail_is_the_first_stderr_line_else_stdout() -> None:
    def make(out: str, err: str):
        return lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, out, err)

    kw = {"app_url": "https://x.example.com", "ambient_env": {}}
    m = _checks_manifest(run="x")
    both = run_hook_slot(
        m, "smoke", runner=make("collected 10\n.F", "\nAssertionError: boom"), **kw
    )
    assert both.checks[0].detail == "AssertionError: boom"
    only_out = run_hook_slot(m, "smoke", runner=make("only out\nmore", ""), **kw)
    assert only_out.checks[0].detail == "only out"


# --- G1 (Hook Grid policy table, schema 3, run_cell, skipped verdicts) --------------------------


_TODO_PROBE = 'python3 probe_health.py "${TARGET_URL:-$APP_URL}"'
_TODO_REQ = f"{_TODO_PROBE} --require-service platform/todo"
_TODO_GATE = f'{_TODO_REQ} --gate "$EXPECTED_SHA"'

_TODO_V2: dict[str, object] = {
    "schema_version": 2,
    "service": "platform/todo",
    "slots": {
        "smoke": {
            "run": _TODO_PROBE,
            "timeout_seconds": 60,
            "required_env": ["APP_URL"],
            "optional_env": ["TARGET_URL"],
        },
        "preview_gate": {
            "run": _TODO_REQ,
            "timeout_seconds": 120,
            "required_env": ["APP_URL"],
            "optional_env": ["TARGET_URL"],
        },
        "staging_gate": {
            "run": _TODO_GATE,
            "timeout_seconds": 180,
            "required_env": ["APP_URL", "EXPECTED_SHA"],
            "optional_env": ["TARGET_URL"],
        },
        "prod_gate": {
            "run": _TODO_GATE,
            "timeout_seconds": 120,
            "required_env": ["APP_URL", "EXPECTED_SHA"],
            "optional_env": ["TARGET_URL"],
        },
        "scheduled_audit": {
            "run": _TODO_REQ,
            "timeout_seconds": 300,
            "required_env": ["APP_URL"],
            "optional_env": ["TARGET_URL"],
        },
    },
}

_TODO_V3: dict[str, object] = {
    "schema_version": 3,
    "service": "platform/todo",
    "required_env": ["APP_URL"],
    "optional_env": ["TARGET_URL"],
    "checks": [
        {
            "id": "smoke",
            "argv": ["sh", "-lc", _TODO_PROBE],
            "needs": ["running_service"],
            "writes": False,
            "accepts_scale": False,
            "timeout_seconds": 60,
        },
        {
            "id": "preview_gate",
            "argv": ["sh", "-lc", _TODO_REQ],
            "needs": ["running_service"],
            "writes": False,
            "accepts_scale": False,
            "timeout_seconds": 120,
            "cells": {
                "remove": {
                    "staging.post": {"reason": "staging uses --gate", "issue": "infra2#1644"},
                    "prod.post": {"reason": "prod uses --gate", "issue": "infra2#1644"},
                    "scheduled_audit": {"reason": "dedicated audit row", "issue": "infra2#1644"},
                }
            },
        },
        {
            "id": "staging_gate",
            "argv": ["sh", "-lc", _TODO_GATE],
            "needs": ["running_service"],
            "writes": False,
            "accepts_scale": False,
            "timeout_seconds": 180,
            "required_env": ["EXPECTED_SHA"],
            "cells": {
                "remove": {
                    "preview.post": {"reason": "preview skips --gate", "issue": "infra2#1644"},
                    "prod.post": {"reason": "prod uses 120s timeout", "issue": "infra2#1644"},
                    "scheduled_audit": {"reason": "audit skips --gate", "issue": "infra2#1644"},
                }
            },
        },
        {
            "id": "prod_gate",
            "argv": ["sh", "-lc", _TODO_GATE],
            "needs": ["running_service"],
            "writes": False,
            "accepts_scale": False,
            "timeout_seconds": 120,
            "required_env": ["EXPECTED_SHA"],
            "cells": {
                "remove": {
                    "preview.post": {"reason": "preview skips --gate", "issue": "infra2#1644"},
                    "staging.post": {"reason": "staging uses 180s timeout", "issue": "infra2#1644"},
                    "scheduled_audit": {"reason": "audit skips --gate", "issue": "infra2#1644"},
                }
            },
        },
        {
            "id": "scheduled_audit",
            "argv": ["sh", "-lc", _TODO_REQ],
            "needs": ["running_service"],
            "writes": False,
            "accepts_scale": False,
            "timeout_seconds": 300,
            "cells": {
                "remove": {
                    "preview.post": {"reason": "dedicated preview row", "issue": "infra2#1644"},
                    "staging.post": {"reason": "staging uses --gate", "issue": "infra2#1644"},
                    "prod.post": {"reason": "prod uses --gate", "issue": "infra2#1644"},
                }
            },
        },
    ],
}

_FR_OPT_ENV = [
    "AI_PROVIDER",
    "AI_BASE_URL",
    "LARGE_MODEL",
    "SMALL_MODEL",
    "MULTIMODAL_MODEL",
]
_FR_BOOT = "uv pip install --system -r tests/e2e/requirements.txt && playwright install chromium"

_FINANCE_REPORT_V2: dict[str, object] = {
    "schema_version": 2,
    "service": "finance_report/app",
    "slots": {
        "smoke": {
            "run": 'bash common/runtime/shell/smoke_test.sh "$INFRA2_HOOK_APP_URL"',
            "timeout_seconds": 120,
        },
        "preview_gate": {
            "bootstrap": _FR_BOOT,
            "checks": [
                {
                    "id": "smoke",
                    "argv": [
                        "sh",
                        "-c",
                        'bash common/runtime/shell/smoke_test.sh "$INFRA2_HOOK_APP_URL"',
                    ],
                },
                {
                    "id": "preview_minimum_e2e",
                    "argv": [
                        "python",
                        "-m",
                        "common.testing.test_selection",
                        "--stage",
                        "preview_minimum_e2e",
                        "--run",
                    ],
                },
                {
                    "id": "touched_files",
                    "argv": [
                        "python",
                        "-m",
                        "common.testing.test_selection",
                        "--touched-files",
                        "--run",
                    ],
                },
            ],
            "timeout_seconds": 360,
            "optional_env": ["INFRA2_TOUCHED_FILES", *_FR_OPT_ENV],
        },
        "staging_gate": {
            "bootstrap": _FR_BOOT,
            "checks": [
                {
                    "id": "smoke",
                    "argv": [
                        "sh",
                        "-c",
                        'bash common/runtime/shell/smoke_test.sh "$INFRA2_HOOK_APP_URL"',
                    ],
                },
                {
                    "id": "staging_core_e2e",
                    "argv": [
                        "python",
                        "-m",
                        "common.testing.test_selection",
                        "--stage",
                        "staging_core_e2e",
                        "--run",
                    ],
                },
                {
                    "id": "ai_provider_connectivity",
                    "argv": [
                        "env",
                        "STRICT_E2E_GATES=1",
                        "python",
                        "-m",
                        "pytest",
                        "tests/e2e/test_ai_provider_connectivity.py",
                        "-v",
                        "--tb=short",
                        "-o",
                        "addopts=",
                    ],
                },
                {
                    "id": "staging_ai_ocr",
                    "argv": [
                        "python",
                        "-m",
                        "common.testing.test_selection",
                        "--stage",
                        "staging_ai_ocr",
                        "--corpus",
                        "canary",
                        "--run",
                    ],
                },
            ],
            "timeout_seconds": 540,
            "optional_env": [*_FR_OPT_ENV, "TEST_USER_EMAIL", "TEST_USER_PASSWORD"],
        },
        "prod_gate": {
            "run": 'bash common/runtime/shell/smoke_test.sh "$INFRA2_HOOK_APP_URL"',
            "timeout_seconds": 180,
        },
        "scheduled_audit": {
            "bootstrap": _FR_BOOT,
            "checks": [
                {
                    "id": "audit_replay",
                    "argv": [
                        "python",
                        "-m",
                        "common.testing.test_selection",
                        "--stage",
                        "staging_ai_ocr",
                        "--corpus",
                        "audit_replay",
                        "--run",
                    ],
                },
                {
                    "id": "core_journey",
                    "argv": [
                        "python",
                        "-m",
                        "common.testing.test_selection",
                        "--stage",
                        "staging_ai_ocr",
                        "--corpus",
                        "core_journey",
                        "--run",
                    ],
                },
                {
                    "id": "pdf_corpus_oracle",
                    "argv": [
                        "sh",
                        "-c",
                        (
                            "python -m common.testing.benchmarks.pdf_corpus_oracle "
                            '--app-url "$INFRA2_HOOK_APP_URL" --max-workers 1'
                        ),
                    ],
                },
                {
                    "id": "financial_scenario_benchmark",
                    "argv": [
                        "sh",
                        "-c",
                        (
                            "python -m common.testing.benchmarks."
                            "run_financial_scenario_benchmark "
                            '--app-url "$INFRA2_HOOK_APP_URL" --case 1,2,3,4,5,6'
                        ),
                    ],
                },
            ],
            "timeout_seconds": 3600,
            "optional_env": [*_FR_OPT_ENV, "TEST_USER_EMAIL", "TEST_USER_PASSWORD"],
        },
    },
}

_FINANCE_REPORT_V3: dict[str, object] = {
    "schema_version": 3,
    "service": "finance_report/app",
    "optional_env": ["INFRA2_TOUCHED_FILES", *_FR_OPT_ENV, "TEST_USER_EMAIL", "TEST_USER_PASSWORD"],
    "checks": [
        {
            "id": "smoke",
            "argv": [
                "sh",
                "-c",
                'bash common/runtime/shell/smoke_test.sh "$INFRA2_HOOK_APP_URL"',
            ],
            "needs": ["running_service"],
            "writes": False,
            "accepts_scale": False,
            "timeout_seconds": 120,
            "cells": {
                "add": {
                    "preview.post": {"reason": "runs inside preview_gate", "issue": "infra2#1644"},
                    "staging.post": {"reason": "runs inside staging_gate", "issue": "infra2#1644"},
                }
            },
        },
        {
            "id": "preview_minimum_e2e",
            "argv": [
                "python",
                "-m",
                "common.testing.test_selection",
                "--stage",
                "preview_minimum_e2e",
                "--run",
            ],
            "needs": ["running_service"],
            "writes": True,
            "accepts_scale": False,
            "timeout_seconds": 120,
            "bootstrap": _FR_BOOT,
            "cells": {
                "remove": {
                    "staging.post": {"reason": "preview only", "issue": "infra2#1644"},
                    "scheduled_audit": {"reason": "preview only", "issue": "infra2#1644"},
                }
            },
        },
        {
            "id": "touched_files",
            "argv": [
                "python",
                "-m",
                "common.testing.test_selection",
                "--touched-files",
                "--run",
            ],
            "needs": ["running_service"],
            "writes": True,
            "accepts_scale": False,
            "timeout_seconds": 120,
            "bootstrap": _FR_BOOT,
            "cells": {
                "remove": {
                    "staging.post": {"reason": "preview only", "issue": "infra2#1644"},
                    "scheduled_audit": {"reason": "preview only", "issue": "infra2#1644"},
                }
            },
        },
        {
            "id": "staging_core_e2e",
            "argv": [
                "python",
                "-m",
                "common.testing.test_selection",
                "--stage",
                "staging_core_e2e",
                "--run",
            ],
            "needs": ["running_service", "staging_data"],
            "writes": True,
            "accepts_scale": False,
            "timeout_seconds": 180,
            "bootstrap": _FR_BOOT,
            "cells": {
                "remove": {
                    "scheduled_audit": {"reason": "tag deploy gate only", "issue": "infra2#1644"},
                }
            },
        },
        {
            "id": "ai_provider_connectivity",
            "argv": [
                "env",
                "STRICT_E2E_GATES=1",
                "python",
                "-m",
                "pytest",
                "tests/e2e/test_ai_provider_connectivity.py",
                "-v",
                "--tb=short",
                "-o",
                "addopts=",
            ],
            "needs": ["running_service", "real_llm"],
            "writes": False,
            "accepts_scale": False,
            "timeout_seconds": 60,
            "bootstrap": _FR_BOOT,
            "cells": {
                "remove": {
                    "preview.post": {"reason": "staging gate only", "issue": "infra2#1644"},
                    "prod.post": {"reason": "staging gate only", "issue": "infra2#1644"},
                    "scheduled_audit": {"reason": "staging gate only", "issue": "infra2#1644"},
                }
            },
        },
        {
            "id": "staging_ai_ocr",
            "argv": [
                "python",
                "-m",
                "common.testing.test_selection",
                "--stage",
                "staging_ai_ocr",
                "--corpus",
                "canary",
                "--run",
            ],
            "needs": ["running_service", "real_llm", "staging_data"],
            "writes": True,
            "accepts_scale": True,
            "timeout_seconds": 180,
            "bootstrap": _FR_BOOT,
            "cells": {
                "remove": {
                    "scheduled_audit": {"reason": "tag deploy canary only", "issue": "infra2#1644"},
                }
            },
        },
        {
            "id": "prod_gate",
            "argv": [
                "sh",
                "-lc",
                'bash common/runtime/shell/smoke_test.sh "$INFRA2_HOOK_APP_URL"',
            ],
            "needs": ["running_service"],
            "writes": False,
            "accepts_scale": False,
            "timeout_seconds": 180,
            "cells": {
                "remove": {
                    "preview.post": {"reason": "prod gate only", "issue": "infra2#1644"},
                    "staging.post": {"reason": "prod gate only", "issue": "infra2#1644"},
                    "scheduled_audit": {"reason": "prod gate only", "issue": "infra2#1644"},
                }
            },
        },
        {
            "id": "audit_replay",
            "argv": [
                "python",
                "-m",
                "common.testing.test_selection",
                "--stage",
                "staging_ai_ocr",
                "--corpus",
                "audit_replay",
                "--run",
            ],
            "needs": ["running_service", "real_llm", "staging_data"],
            "writes": True,
            "accepts_scale": True,
            "timeout_seconds": 900,
            "bootstrap": _FR_BOOT,
            "cells": {
                "remove": {
                    "staging.post": {"reason": "nightly audit only", "issue": "infra2#1644"},
                }
            },
        },
        {
            "id": "core_journey",
            "argv": [
                "python",
                "-m",
                "common.testing.test_selection",
                "--stage",
                "staging_ai_ocr",
                "--corpus",
                "core_journey",
                "--run",
            ],
            "needs": ["running_service", "real_llm", "staging_data"],
            "writes": True,
            "accepts_scale": True,
            "timeout_seconds": 900,
            "bootstrap": _FR_BOOT,
            "cells": {
                "remove": {
                    "staging.post": {"reason": "nightly audit only", "issue": "infra2#1644"},
                }
            },
        },
        {
            "id": "pdf_corpus_oracle",
            "argv": [
                "sh",
                "-c",
                (
                    "python -m common.testing.benchmarks.pdf_corpus_oracle "
                    '--app-url "$INFRA2_HOOK_APP_URL" --max-workers 1'
                ),
            ],
            "needs": ["running_service", "real_llm", "staging_data"],
            "writes": True,
            "accepts_scale": True,
            "timeout_seconds": 900,
            "bootstrap": _FR_BOOT,
            "cells": {
                "remove": {
                    "staging.post": {"reason": "nightly audit only", "issue": "infra2#1644"},
                }
            },
        },
        {
            "id": "financial_scenario_benchmark",
            "argv": [
                "sh",
                "-c",
                (
                    "python -m common.testing.benchmarks."
                    "run_financial_scenario_benchmark "
                    '--app-url "$INFRA2_HOOK_APP_URL" --case 1,2,3,4,5,6'
                ),
            ],
            "needs": ["running_service", "real_llm", "staging_data"],
            "writes": True,
            "accepts_scale": True,
            "timeout_seconds": 900,
            "bootstrap": _FR_BOOT,
            "cells": {
                "remove": {
                    "staging.post": {"reason": "nightly audit only", "issue": "infra2#1644"},
                }
            },
        },
    ],
}

_TA_BOOT = (
    "cd apps/app-web && bun install --frozen-lockfile && "
    "bunx playwright install --with-deps chromium"
)
_TA_ENV = [
    "APP_URL",
    "APP_VERSION_REF",
    "TA_EMAIL",
    "TA_MEMBER_EMAIL",
    "TA_PASSWORD",
    "TA_MCP_TOKEN",
]
_TA_HEALTH_CMD = (
    'uv run python -m infra2_sdk.deploy --url "$APP_URL/api/health" '
    '--expected-version "$APP_VERSION_REF" --max-attempts 36 '
    "--interval-seconds 5 --no-follow-redirects"
)
_TA_PROD_CMD = (
    'uv run python -m infra2_sdk.deploy --url "$APP_URL/api/health" '
    "--max-attempts 12 --interval-seconds 5 --no-follow-redirects"
)
_TA_NIGHTLY_CMD = (
    'uv run python -m truealpha_tools.nightly_verdicts "$APP_URL/api/health" '
    '--environment "$ENVIRONMENT" --repo .'
)
_TA_CHECKS = [
    {"id": "health", "argv": ["sh", "-c", _TA_HEALTH_CMD]},
    {
        "id": "warm_surface",
        "argv": ["sh", "-c", 'bash tools/warm_surface.sh "$APP_URL" / /research/rankings'],
    },
    {
        "id": "walk_tree",
        "argv": ["sh", "-c", 'cd apps/app-web && TA_BASE_URL="$APP_URL" node e2e/walk-tree.mjs'],
    },
]

_TRUEALPHA_V2: dict[str, object] = {
    "schema_version": 2,
    "service": "truealpha/app",
    "slots": {
        "smoke": {
            "run": _TA_HEALTH_CMD,
            "timeout_seconds": 120,
            "required_env": ["APP_URL", "APP_VERSION_REF"],
        },
        "preview_gate": {
            "bootstrap": _TA_BOOT,
            "timeout_seconds": 360,
            "required_env": _TA_ENV,
            "checks": _TA_CHECKS,
        },
        "staging_gate": {
            "bootstrap": _TA_BOOT,
            "timeout_seconds": 420,
            "required_env": _TA_ENV,
            "checks": _TA_CHECKS,
        },
        "prod_gate": {
            "run": _TA_PROD_CMD,
            "timeout_seconds": 240,
            "required_env": ["APP_URL"],
        },
        "scheduled_audit": {
            "bootstrap": "uv sync --frozen",
            "timeout_seconds": 600,
            "required_env": ["APP_URL", "ENVIRONMENT"],
            "checkout_lineage": True,
            "checks": [{"id": "nightly_verdicts", "argv": ["sh", "-c", _TA_NIGHTLY_CMD]}],
        },
    },
}

_TRUEALPHA_V3: dict[str, object] = {
    "schema_version": 3,
    "service": "truealpha/app",
    "required_env": ["APP_URL"],
    "checks": [
        {
            "id": "smoke",
            "argv": ["sh", "-lc", _TA_HEALTH_CMD],
            "needs": ["running_service"],
            "writes": False,
            "accepts_scale": False,
            "timeout_seconds": 120,
            "required_env": ["APP_VERSION_REF"],
        },
        {
            "id": "health",
            "argv": ["sh", "-c", _TA_HEALTH_CMD],
            "needs": ["running_service"],
            "writes": False,
            "accepts_scale": False,
            "timeout_seconds": 120,
            "bootstrap": _TA_BOOT,
            "required_env": _TA_ENV[1:],
            "cells": {
                "remove": {
                    "prod.post": {"reason": "prod uses prod_gate", "issue": "infra2#1644"},
                    "scheduled_audit": {
                        "reason": "audit uses nightly_verdicts",
                        "issue": "infra2#1644",
                    },
                }
            },
        },
        {
            "id": "warm_surface",
            "argv": ["sh", "-c", 'bash tools/warm_surface.sh "$APP_URL" / /research/rankings'],
            "needs": ["running_service"],
            "writes": False,
            "accepts_scale": False,
            "timeout_seconds": 60,
            "bootstrap": _TA_BOOT,
            "required_env": _TA_ENV[1:],
            "cells": {
                "remove": {
                    "prod.post": {"reason": "prod uses prod_gate", "issue": "infra2#1644"},
                    "scheduled_audit": {
                        "reason": "audit uses nightly_verdicts",
                        "issue": "infra2#1644",
                    },
                }
            },
        },
        {
            "id": "walk_tree",
            "argv": [
                "sh",
                "-c",
                'cd apps/app-web && TA_BASE_URL="$APP_URL" node e2e/walk-tree.mjs',
            ],
            "needs": ["running_service"],
            "writes": True,
            "accepts_scale": False,
            "timeout_seconds": 240,
            "bootstrap": _TA_BOOT,
            "required_env": _TA_ENV[1:],
            "cells": {
                "remove": {
                    "scheduled_audit": {
                        "reason": "audit uses nightly_verdicts",
                        "issue": "infra2#1644",
                    },
                }
            },
        },
        {
            "id": "prod_gate",
            "argv": ["sh", "-lc", _TA_PROD_CMD],
            "needs": ["running_service"],
            "writes": False,
            "accepts_scale": False,
            "timeout_seconds": 240,
            "cells": {
                "remove": {
                    "preview.post": {"reason": "prod gate only", "issue": "infra2#1644"},
                    "staging.post": {"reason": "prod gate only", "issue": "infra2#1644"},
                    "scheduled_audit": {"reason": "prod gate only", "issue": "infra2#1644"},
                }
            },
        },
        {
            "id": "nightly_verdicts",
            "argv": ["sh", "-c", _TA_NIGHTLY_CMD],
            "needs": ["running_service", "staging_data"],
            "writes": False,
            "accepts_scale": False,
            "timeout_seconds": 600,
            "bootstrap": "uv sync --frozen",
            "required_env": ["ENVIRONMENT"],
            "checkout_lineage": True,
            "cells": {
                "remove": {
                    "staging.post": {"reason": "clock audit only", "issue": "infra2#1644"},
                }
            },
        },
    ],
}


def _slot_projection(
    m: RuntimeHooksManifest,
) -> dict[str, tuple[list[str], list[tuple[str, ...]], int]]:
    out: dict[str, tuple[list[str], list[tuple[str, ...]], int]] = {}
    for slot in HOOK_SLOTS:
        spec = m.slots[slot]
        steps = spec.checks or (CheckSpec(slot, ("sh", "-lc", spec.run)),)
        norm_argvs = [
            ("sh", "-c", c.argv[2]) if len(c.argv) == 3 and c.argv[:2] == ("sh", "-lc") else c.argv
            for c in steps
        ]
        out[slot] = ([c.id for c in steps], norm_argvs, spec.timeout_seconds)
    return out


def _cell_projection(m: RuntimeHooksManifest) -> dict[str, list[str]]:
    cells = (
        "preview.operation",
        "staging.operation",
        "prod.operation",
        "preview.post",
        "staging.post",
        "prod.post",
        "scheduled_audit",
    )
    return {cid: [c.id for c in m.checks_for_cell(cid)] for cid in cells}


@pytest.mark.parametrize(
    ("name", "v2_dict", "v3_dict"),
    [
        ("platform_todo", _TODO_V2, _TODO_V3),
        ("finance_report", _FINANCE_REPORT_V2, _FINANCE_REPORT_V3),
        ("truealpha", _TRUEALPHA_V2, _TRUEALPHA_V3),
    ],
)
def test_g1_ac1_schema_3_matches_schema_2_per_slot_and_fails_when_cell_lost(
    name: str, v2_dict: dict[str, object], v3_dict: dict[str, object]
) -> None:
    m2 = RuntimeHooksManifest.from_dict(v2_dict)
    m3 = RuntimeHooksManifest.from_dict(v3_dict)
    assert m3.schema_version == 3
    assert RuntimeHooksManifest.from_dict(m3.to_dict()) == m3
    assert _slot_projection(m3) == _slot_projection(m2), f"{name}: v3 projection drifted from v2"
    assert _cell_projection(m3) == _cell_projection(m2), f"{name}: cell projection drifted from v2"

    # Non-smoke checks in truealpha/platform_todo also match verbatim without sh -lc normalization.
    for slot in HOOK_SLOTS:
        if name == "finance_report" and slot == "smoke":
            continue
        s2 = m2.slots[slot].checks or (CheckSpec(slot, ("sh", "-lc", m2.slots[slot].run)),)
        s3 = m3.slots[slot].checks
        assert [(c.id, c.argv) for c in s3] == [(c.id, c.argv) for c in s2]

    # Mutation check: removing one cell from one row must cause the projection comparison to fail.
    mutated = json.loads(json.dumps(v3_dict))
    row0 = mutated["checks"][0]
    row0_cells = row0.setdefault("cells", {})
    if "preview.post" in row0_cells.get("add", {}):
        del row0_cells["add"]["preview.post"]
    else:
        row0_cells.setdefault("remove", {})["preview.operation"] = {
            "reason": "mutation",
            "issue": "test#1",
        }
    m3_mut = RuntimeHooksManifest.from_dict(mutated)
    assert _cell_projection(m3_mut) != _cell_projection(m2), (
        f"{name}: losing one cell did not fail projection comparison"
    )
    # Also mutate a post row (row 1) and verify slot projection itself fails.
    mutated_post = json.loads(json.dumps(v3_dict))
    row1 = mutated_post["checks"][1]
    row1.setdefault("cells", {}).setdefault("remove", {})["preview.post"] = {
        "reason": "mutation",
        "issue": "test#1",
    }
    m3_mut_post = RuntimeHooksManifest.from_dict(mutated_post)
    assert set(m3_mut_post.slots) != set(m2.slots) or _slot_projection(
        m3_mut_post
    ) != _slot_projection(m2)


def test_g1_ac2_run_cell_stops_on_local_pre_and_runs_all_in_post_cells(tmp_path: Path) -> None:
    manifest = RuntimeHooksManifest.from_dict(
        {
            "schema_version": 3,
            "service": "demo/app",
            "checks": [
                {
                    "id": "lint",
                    "argv": ["lint"],
                    "needs": ["code"],
                    "writes": False,
                    "accepts_scale": False,
                    "timeout_seconds": 10,
                },
                {
                    "id": "unit",
                    "argv": ["unit"],
                    "needs": ["code"],
                    "writes": False,
                    "accepts_scale": False,
                    "timeout_seconds": 10,
                },
                {
                    "id": "e2e_a",
                    "argv": ["e2e_a"],
                    "needs": ["running_service"],
                    "writes": False,
                    "accepts_scale": True,
                    "timeout_seconds": 30,
                },
                {
                    "id": "e2e_b",
                    "argv": ["e2e_b"],
                    "needs": ["running_service"],
                    "writes": False,
                    "accepts_scale": False,
                    "timeout_seconds": 30,
                },
                {
                    "id": "e2e_c",
                    "argv": ["e2e_c"],
                    "needs": ["running_service"],
                    "writes": False,
                    "accepts_scale": False,
                    "timeout_seconds": 30,
                },
            ],
        }
    )

    calls: list[list[str]] = []

    def fail_first_runner(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[0] in ("lint", "e2e_a"):
            return subprocess.CompletedProcess(cmd, 4, "", "failed check")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    # local.pre stops at first failure (unit never runs)
    local_res = run_cell(
        manifest, "local", "pre", cwd=tmp_path, ambient_env={}, runner=fail_first_runner
    )
    assert calls == [["lint"]]
    assert (local_res.status, local_res.exit_code, local_res.cell) == ("fail", 4, "local.pre")
    assert [(c.name, c.ok) for c in local_res.checks] == [("lint", False)]

    # All post cells run EVERY active check even when the first fails
    for stage, phase, expected_cell in (
        ("preview", "post", "preview.post"),
        ("staging", "post", "staging.post"),
        ("prod", "post", "prod.post"),
        ("scheduled_audit", None, "scheduled_audit"),
    ):
        calls.clear()
        seen_envs: list[dict[str, str]] = []

        def post_runner(
            cmd: list[str], *, seen: list[dict[str, str]] = seen_envs, **kw: object
        ) -> subprocess.CompletedProcess[str]:
            seen.append(dict(kw["env"]))  # type: ignore[arg-type]
            return fail_first_runner(cmd, **kw)

        res = run_cell(
            manifest,
            stage,
            phase,
            app_url="https://app.example.com",
            cwd=tmp_path,
            ambient_env={},
            runner=post_runner,
        )
        assert calls == [["e2e_a"], ["e2e_b"], ["e2e_c"]]
        assert (res.status, res.exit_code, res.cell, res.policy_version) == (
            "fail",
            4,
            expected_cell,
            POLICY_VERSION,
        )
        assert [(c.name, c.ok, c.skipped) for c in res.checks] == [
            ("e2e_a", False, False),
            ("e2e_b", True, False),
            ("e2e_c", True, False),
        ]
        assert seen_envs[0]["HOOK_SCALE"] == CELL_POLICIES[expected_cell].scale


def test_g1_ac3_off_rows_emit_skipped_and_zero_active_checks_fails(tmp_path: Path) -> None:
    manifest = RuntimeHooksManifest.from_dict(
        {
            "schema_version": 3,
            "service": "demo/app",
            "checks": [
                {
                    "id": "active_check",
                    "argv": ["ok_cmd"],
                    "needs": ["running_service"],
                    "writes": False,
                    "accepts_scale": False,
                    "timeout_seconds": 30,
                },
                {
                    "id": "disabled_check",
                    "argv": ["skip_cmd"],
                    "needs": ["running_service"],
                    "writes": False,
                    "accepts_scale": False,
                    "timeout_seconds": 30,
                    "off": {"reason": "flaky upstream", "issue": "infra2#999"},
                },
            ],
        }
    )
    calls: list[list[str]] = []

    def ok_runner(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    res = run_cell(
        manifest,
        "preview",
        "post",
        app_url="https://app.example.com",
        cwd=tmp_path,
        ambient_env={},
        runner=ok_runner,
    )
    assert calls == [["ok_cmd"]]
    assert res.status == "pass"
    assert [(c.name, c.status, c.ok, c.skipped) for c in res.checks] == [
        ("active_check", "pass", True, False),
        ("disabled_check", "skipped", False, True),
    ]

    # Cell with only off checks (0 active checks) fails
    all_off = RuntimeHooksManifest.from_dict(
        {
            "schema_version": 3,
            "service": "demo/app",
            "checks": [
                {
                    "id": "disabled_only",
                    "argv": ["skip_cmd"],
                    "needs": ["code"],
                    "writes": False,
                    "accepts_scale": False,
                    "timeout_seconds": 10,
                    "off": {"reason": "temporarily off", "issue": "infra2#1000"},
                }
            ],
        }
    )
    calls.clear()
    off_res = run_cell(all_off, "local", "pre", cwd=tmp_path, ambient_env={}, runner=ok_runner)
    assert calls == []
    assert off_res.status == "fail"
    assert "0 active checks" in off_res.summary
    assert [(c.name, c.status, c.skipped) for c in off_res.checks] == [
        ("disabled_only", "skipped", True)
    ]

    # Cell with 0 scheduled checks fails
    empty_cell_res = run_cell(
        all_off,
        "preview",
        "post",
        app_url="https://app.example.com",
        cwd=tmp_path,
        ambient_env={},
        runner=ok_runner,
    )
    assert empty_cell_res.status == "fail"
    assert "0 active checks" in empty_cell_res.summary


def test_g1_ac4_scheduler_computes_defaults_and_refuses_missing_capabilities() -> None:
    code_check = CheckRow(id="lint", argv=("ruff", "check"), needs=("code",))
    assert resolve_check_cells(code_check) == ("local.pre", "ci.pre")

    rw_service_check = CheckRow(id="e2e", argv=("pytest",), needs=("running_service",), writes=True)
    assert resolve_check_cells(rw_service_check) == (
        "preview.post",
        "staging.post",
        "scheduled_audit",
    )

    staging_check = CheckRow(
        id="oracle", argv=("oracle",), needs=("running_service", "staging_data"), writes=False
    )
    assert resolve_check_cells(staging_check) == ("staging.post", "scheduled_audit")

    # real_llm refused in local and ci
    for bad_cell in ("local.pre", "local.post", "ci.pre", "ci.post"):
        with pytest.raises(ValueError, match="real_llm"):
            CheckRow(
                id="llm_check",
                argv=("pytest",),
                needs=("running_service", "real_llm"),
                cells=CellOverrides(add={bad_cell: OverrideReason("want local", "infra2#1")}),
            )

    # writes=True refused in prod
    with pytest.raises(ValueError, match="writes=True"):
        CheckRow(
            id="mutating",
            argv=("pytest",),
            needs=("running_service",),
            writes=True,
            cells=CellOverrides(add={"prod.post": OverrideReason("want prod", "infra2#1")}),
        )

    # staging_data refused in preview.post and prod.post
    for bad_cell in ("preview.post", "prod.post"):
        with pytest.raises(ValueError, match="staging_data"):
            CheckRow(
                id="needs_staging",
                argv=("pytest",),
                needs=("running_service", "staging_data"),
                cells=CellOverrides(add={bad_cell: OverrideReason("want", "infra2#1")}),
            )

    # smoke with bootstrap refused
    with pytest.raises(ValueError, match="bootstrap"):
        CheckRow(
            id="smoke",
            argv=("sh", "-c", "true"),
            needs=("running_service",),
            bootstrap="pip install x",
        )


def test_g1_policy_table_completeness_derived_views_and_grid_printer(tmp_path: Path) -> None:
    expected_15 = {
        f"{st}.{ph}"
        for st in ("local", "ci", "preview", "staging", "prod")
        for ph in ("pre", "operation", "post")
    }
    assert set(CELL_POLICIES) == expected_15 | {"scheduled_audit"}

    # Views are derived from stages.yaml, not hardcoded literals.
    yaml_copy = tmp_path / "stages.yaml"
    base_yaml = (
        Path(__file__).resolve().parents[1] / "src" / "infra2_sdk" / "data" / "stages.yaml"
    ).read_text(encoding="utf-8")
    yaml_copy.write_text(
        base_yaml.replace("budget_seconds: 360\n", "budget_seconds: 444\n"), encoding="utf-8"
    )
    pv, policies = _load_policy_table(yaml_copy)
    assert pv == 1
    _, timeouts, _ = _derive_views(policies)
    assert timeouts["preview_gate"] == 444

    # format_check_grid prints checks x cells for both v3 and v2 manifests.
    m3 = RuntimeHooksManifest.from_dict(_TRUEALPHA_V3)
    grid3 = format_check_grid(m3)
    assert "Service: truealpha/app (schema v3, policy v1)" in grid3
    assert "`health`" in grid3 and "`nightly_verdicts`" in grid3
    assert "preview.post" in grid3 and "local.pre" in grid3

    m2 = RuntimeHooksManifest.from_dict(_TRUEALPHA_V2)
    grid2 = format_check_grid(m2)
    assert "Service: truealpha/app (schema v2, policy v1)" in grid2
    assert "`health`" in grid2 and "`nightly_verdicts`" in grid2


def test_run_hook_slot_stays_stop_on_fail_while_run_cell_runs_all_post_checks(
    tmp_path: Path,
) -> None:
    raw = _sample_v2_manifest_dict()
    raw["slots"] = {  # type: ignore[index]
        **raw["slots"],  # type: ignore[dict-item]
        "preview_gate": {
            "checks": [{"id": "first", "argv": ["first"]}, {"id": "second", "argv": ["second"]}],
            "timeout_seconds": 120,
        },
    }
    manifest = RuntimeHooksManifest.from_dict(raw)
    calls: list[list[str]] = []

    def fail_first(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        rc = 2 if cmd[0] == "first" else 0
        return subprocess.CompletedProcess(cmd, rc, "", "boom" if rc else "")

    hook_res = run_hook_slot(
        manifest,
        "preview_gate",
        app_url="https://app.example.com",
        cwd=tmp_path,
        ambient_env={},
        runner=fail_first,
    )
    assert calls == [["first"]]
    assert (hook_res.status, len(hook_res.checks)) == ("fail", 1)

    calls.clear()
    cell_res = run_cell(
        manifest,
        "preview",
        "post",
        app_url="https://app.example.com",
        cwd=tmp_path,
        ambient_env={},
        runner=fail_first,
    )
    assert calls == [["first"], ["second"]]
    assert [(c.name, c.ok) for c in cell_res.checks] == [("first", False), ("second", True)]


def test_check_timeout_detail_reports_check_timeout_vs_cell_budget(tmp_path: Path) -> None:
    manifest = RuntimeHooksManifest.from_dict(
        {
            "schema_version": 3,
            "service": "demo/app",
            "checks": [
                {
                    "id": "fast_cap",
                    "argv": ["fast_cap"],
                    "needs": ["running_service"],
                    "writes": False,
                    "accepts_scale": False,
                    "timeout_seconds": 10,
                },
                {
                    "id": "larger_cap",
                    "argv": ["larger_cap"],
                    "needs": ["running_service"],
                    "writes": False,
                    "accepts_scale": False,
                    "timeout_seconds": 200,
                },
            ],
        }
    )

    def timeout_first(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "fast_cap":
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=10, stderr="slow")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    res = run_cell(
        manifest,
        "preview",
        "post",
        app_url="https://app.example.com",
        cwd=tmp_path,
        ambient_env={},
        runner=timeout_first,
    )
    assert res.status == "fail"
    assert "check timeout 10s" in res.checks[0].detail
    assert "10s" in res.summary
    assert res.checks[1].ok is True
