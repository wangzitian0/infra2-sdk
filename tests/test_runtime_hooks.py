"""Falsifiable unit tests for runtime verification hooks and next_patch_tag (#170)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

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
    FORBIDDEN_HOOK_ENV_NAMES,
    FORBIDDEN_HOOK_ENV_PREFIXES,
    build_hook_env,
    main,
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
    with pytest.raises(ValueError, match="schema_version 1"):
        RuntimeHooksManifest.from_dict({**base, "schema_version": 2})
    with pytest.raises(ValueError, match="schema_version 1"):
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


def test_cli_check_hooks_and_run_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest_path = tmp_path / "runtime-hooks.manifest.json"
    raw = _sample_manifest_dict(service="truealpha/app")
    raw["slots"]["smoke"] = {  # type: ignore[index]
        "run": 'test "$APP_URL" = "http://127.0.0.1:8000" && test -z "$INFRA2_PAT"',
        "timeout_seconds": 30,
    }
    manifest_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")

    output: list[str] = []
    rc = main(["--check-hooks", "runtime-hooks.manifest.json"], root=tmp_path, echo=output.append)
    assert rc == 0
    assert output == ["runtime hooks manifest: ok (truealpha/app, 4 slots)"]

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
