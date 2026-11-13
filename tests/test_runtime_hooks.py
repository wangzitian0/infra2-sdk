"""Falsifiable unit tests for runtime verification hooks and next_patch_tag (#170)."""

from __future__ import annotations

import json
import subprocess
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

from infra2_sdk import (
    HOOK_SLOTS,
    HookRunResult,
    HookSlotSpec,
    RuntimeHooksManifest,
    load_hooks_manifest,
    next_patch_tag,
    run_hook_slot,
)
from infra2_sdk.manifests import (
    CURRENT_HOOKS_SCHEMA_VERSION,
    FORBIDDEN_HOOK_ENV_NAMES,
    FORBIDDEN_HOOK_ENV_PREFIXES,
    LEGACY_V1_HOOK_SLOTS,
    SLOT_MAX_TIMEOUT_SECONDS,
    STAGE_HOOK_PLANS,
    CheckSpec,
    StageHookPlan,
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
    assert CURRENT_HOOKS_SCHEMA_VERSION == 2
    assert HOOK_SLOTS == ("smoke", "preview_gate", "staging_gate", "prod_gate", "scheduled_audit")
    assert LEGACY_V1_HOOK_SLOTS == ("smoke", "preview_gate", "staging_gate", "scheduled_audit")
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

    # Historical v1 manifests still load and synthesize prod_gate from smoke,
    # while --check-hooks rejects schema_version 1 unless --allow-legacy-schema is passed.
    v1_path = tmp_path / "v1.manifest.json"
    v1_path.write_text(json.dumps(_sample_manifest_dict(), indent=2), encoding="utf-8")
    v1_loaded = load_hooks_manifest("v1.manifest.json", root=tmp_path)
    assert v1_loaded.schema_version == 1
    assert v1_loaded.slots["prod_gate"] == v1_loaded.slots["smoke"]

    out_strict: list[str] = []
    assert main(["--check-hooks", "v1.manifest.json"], root=tmp_path, echo=out_strict.append) == 1
    assert any("schema_version 2" in line for line in out_strict)

    out_legacy: list[str] = []
    assert (
        main(
            ["--check-hooks", "v1.manifest.json", "--allow-legacy-schema"],
            root=tmp_path,
            echo=out_legacy.append,
        )
        == 0
    )


def _sample_manifest_dict(*, service: str = "finance_report/app") -> dict[str, object]:
    return {
        "schema_version": 1,
        "service": service,
        "slots": {
            "smoke": {
                "run": "python -m scripts.smoke",
                "timeout_seconds": 60,
            },
            "preview_gate": {
                "run": "pytest -m e2e_preview",
                "bootstrap": "playwright install chromium",
                "timeout_seconds": 600,
                "required_env": ["E2E_USER_EMAIL"],
                "optional_env": ["E2E_LOCALE"],
                "artifacts_dir": ".artifacts/preview",
            },
            "staging_gate": {
                "run": "pytest -m staging_canary",
                "timeout_seconds": 900,
                "required_env": ["LLM_API_KEY"],
            },
            "scheduled_audit": {
                "run": "python -m scripts.audit_replay",
                "timeout_seconds": 1800,
            },
        },
    }


def test_runtime_hooks_manifest_roundtrip_and_load(tmp_path: Path) -> None:
    raw = _sample_manifest_dict()
    manifest_file = tmp_path / "runtime-hooks.manifest.json"
    manifest_file.write_text(json.dumps(raw, indent=2), encoding="utf-8")

    loaded = load_hooks_manifest("runtime-hooks.manifest.json", root=tmp_path)
    assert loaded.schema_version == 1
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

    base = _sample_manifest_dict()
    with pytest.raises(ValueError, match="missing required slot"):
        RuntimeHooksManifest.from_dict({**base, "schema_version": 2})
    with pytest.raises(ValueError, match="schema_version 1 or 2"):
        RuntimeHooksManifest.from_dict({**base, "schema_version": 3})
    with pytest.raises(ValueError, match="schema_version 1 or 2"):
        RuntimeHooksManifest.from_dict({**base, "schema_version": True})
    with pytest.raises(ValueError, match="cannot declare 'prod_gate'"):
        RuntimeHooksManifest.from_dict(
            {**base, "slots": {**base["slots"], "prod_gate": {"run": "echo ok"}}}  # type: ignore[dict-item]
        )
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
    manifest = RuntimeHooksManifest.from_dict(_sample_manifest_dict())
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
    manifest = RuntimeHooksManifest.from_dict(_sample_manifest_dict())
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
    manifest = RuntimeHooksManifest.from_dict(_sample_manifest_dict())

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
    manifest = RuntimeHooksManifest.from_dict(_sample_manifest_dict())

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
    with pytest.warns(DeprecationWarning, match="v3.31.0"):
        assert plan.deploy_slot == plan.operation_slot
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
