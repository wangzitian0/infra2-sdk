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
    POLICY_VERSION,
    CellOverrides,
    CheckRow,
    HookRunResult,
    OverrideReason,
    RuntimeHooksManifest,
    format_check_grid,
    load_hooks_manifest,
    next_patch_tag,
    resolve_check_cells,
    run_cell,
)
from infra2_sdk.manifests import (
    CURRENT_HOOKS_SCHEMA_VERSION,
    FORBIDDEN_HOOK_ENV_NAMES,
    FORBIDDEN_HOOK_ENV_PREFIXES,
    STAGE_HOOK_PLANS,
    SUPPORTED_HOOKS_SCHEMA_VERSIONS,
    StageHookPlan,
    _derive_stage_plans,
    _load_policy_table,
    build_hook_env,
    main,
    stage_hook_plan,
)
from infra2_sdk.observability import CheckVerdict, load_trail

_SCHEMA_2_REFUSAL = r"schema_version 2 was removed in 4\.0\.0.*schema_version 3.*run_cell"


@pytest.fixture(autouse=True)
def _run_in_tmp_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # run_cell writes <cwd>/.artifacts/hooks/checks.jsonl


def _row(check_id: str, *argv: str, **extra: object) -> dict[str, object]:
    return {
        "id": check_id,
        "argv": list(argv) or [check_id],
        "needs": ["running_service"],
        "writes": False,
        "accepts_scale": False,
        "timeout_seconds": 60,
        **extra,
    }


def _manifest_dict(*rows: dict[str, object], **top: object) -> dict[str, object]:
    return {"schema_version": 3, "service": "finance_report/app", "checks": list(rows), **top}


def _sample_manifest_dict(*, service: str = "finance_report/app") -> dict[str, object]:
    return _manifest_dict(
        _row("smoke", "python", "-m", "scripts.smoke"),
        _row(
            "preview_e2e",
            "pytest",
            "-m",
            "e2e_preview",
            writes=True,
            bootstrap="playwright install chromium",
            timeout_seconds=300,
            required_env=["E2E_USER_EMAIL"],
            optional_env=["E2E_LOCALE"],
            cells={"remove": {"staging.post": {"reason": "preview only", "issue": "t#1"}}},
        ),
        service=service,
        artifacts_dir=".artifacts/preview",
        required_env=["APP_API_TOKEN"],
        optional_env=["OPTIONAL_FLAG"],
    )


def _preview_post_manifest(*rows: dict[str, object]) -> RuntimeHooksManifest:
    return RuntimeHooksManifest.from_dict(_manifest_dict(*rows))


def _run_preview_post(manifest: RuntimeHooksManifest, **kw: object) -> HookRunResult:
    kw.setdefault("app_url", "https://preview.example.com")
    kw.setdefault("environment", "preview")
    kw.setdefault("version_ref", "1234567")
    kw.setdefault("ambient_env", {})
    return run_cell(manifest, "preview", "post", **kw)  # type: ignore[arg-type]


def test_schema_constants_and_stage_hook_plans() -> None:
    assert CURRENT_HOOKS_SCHEMA_VERSION == 3
    assert SUPPORTED_HOOKS_SCHEMA_VERSIONS == (3,)
    assert POLICY_VERSION == 1
    assert not hasattr(_manifests_mod, "LEGACY_V1_HOOK_SLOTS")
    assert stage_hook_plan("production") == STAGE_HOOK_PLANS["prod"]
    with pytest.raises(ValueError, match="unknown stage"):
        stage_hook_plan("canary")
    with pytest.raises(ValueError, match="stage must be a string"):
        stage_hook_plan(123)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        STAGE_HOOK_PLANS["prod"] = StageHookPlan("prod", (), None, None, stage_id="x")  # type: ignore[index]


def test_schema_2_manifest_is_refused_with_schema_3_and_run_cell_message(tmp_path: Path) -> None:
    schema_2 = {
        "schema_version": 2,
        "service": "finance_report/app",
        "slots": {"smoke": {"run": "python -m scripts.smoke", "timeout_seconds": 60}},
    }
    with pytest.raises(ValueError, match=_SCHEMA_2_REFUSAL):
        RuntimeHooksManifest.from_dict(schema_2)
    with pytest.raises(ValueError, match=_SCHEMA_2_REFUSAL):
        RuntimeHooksManifest("finance_report/app", schema_version=2, checks=())

    # A schema 3 body under schema_version 2 is refused for the version, not for its fields.
    with pytest.raises(ValueError, match=_SCHEMA_2_REFUSAL):
        RuntimeHooksManifest.from_dict({**_sample_manifest_dict(), "schema_version": 2})

    (tmp_path / "v2.manifest.json").write_text(json.dumps(schema_2), encoding="utf-8")
    with pytest.raises(ValueError, match=_SCHEMA_2_REFUSAL):
        load_hooks_manifest("v2.manifest.json", root=tmp_path)
    out: list[str] = []
    assert main(["--check-hooks", "v2.manifest.json"], root=tmp_path, echo=out.append) == 1
    assert len(out) == 1 and "schema_version 3" in out[0] and "run_cell" in out[0]


@pytest.mark.parametrize("bad_version", [1, 4, True, "3", None])
def test_unsupported_schema_versions_are_refused(bad_version: object, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires schema_version 3"):
        RuntimeHooksManifest.from_dict({**_sample_manifest_dict(), "schema_version": bad_version})
    path = tmp_path / "bad.manifest.json"
    path.write_text(
        json.dumps({**_sample_manifest_dict(), "schema_version": bad_version}), encoding="utf-8"
    )
    out: list[str] = []
    assert main(["--check-hooks", "bad.manifest.json"], root=tmp_path, echo=out.append) == 1
    assert any("requires schema_version 3" in line for line in out)


def test_runtime_hooks_manifest_roundtrip_and_load(tmp_path: Path) -> None:
    raw = _sample_manifest_dict()
    manifest_file = tmp_path / "runtime-hooks.manifest.json"
    manifest_file.write_text(json.dumps(raw, indent=2), encoding="utf-8")

    loaded = load_hooks_manifest("runtime-hooks.manifest.json", root=tmp_path)
    assert loaded.schema_version == 3
    assert loaded.service == "finance_report/app"
    assert [c.id for c in loaded.checks] == ["smoke", "preview_e2e"]
    assert loaded.checks[1].bootstrap == "playwright install chromium"
    assert loaded.checks[1].required_env == ("E2E_USER_EMAIL",)
    assert loaded.checks[1].optional_env == ("E2E_LOCALE",)
    assert loaded.artifacts_dir == ".artifacts/preview"
    assert loaded.required_env == ("APP_API_TOKEN",)
    assert loaded.optional_env == ("OPTIONAL_FLAG",)
    assert not hasattr(loaded, "slots")
    assert RuntimeHooksManifest.from_dict(loaded.to_dict()) == loaded
    assert loaded.to_dict() == raw | {"checks": loaded.to_dict()["checks"]}
    default_dir = RuntimeHooksManifest.from_dict(_manifest_dict(_row("a")))
    assert "artifacts_dir" not in default_dir.to_dict()
    assert default_dir.artifacts_dir == ".artifacts/hooks"


@pytest.mark.parametrize(
    "bad_secret",
    [
        *sorted(FORBIDDEN_HOOK_ENV_NAMES),
        *(f"{prefix}SECRET" for prefix in FORBIDDEN_HOOK_ENV_PREFIXES),
    ],
)
def test_hook_env_lists_reject_forbidden_platform_secrets(bad_secret: str) -> None:
    for field_name in ("required_env", "optional_env"):
        with pytest.raises(ValueError, match="forbidden platform secret"):
            CheckRow(id="a", argv=("true",), needs=("code",), **{field_name: (bad_secret,)})
        with pytest.raises(ValueError, match="forbidden platform secret"):
            RuntimeHooksManifest.from_dict(_manifest_dict(_row("a"), **{field_name: [bad_secret]}))


@pytest.mark.parametrize(
    "bad_env_name",
    ["", "lowercase", "1LEADING_DIGIT", "BAD-HYPHEN", "SPACE IN NAME"],
)
def test_hook_env_lists_reject_invalid_env_names(bad_env_name: str) -> None:
    with pytest.raises(ValueError, match="must match"):
        CheckRow(id="a", argv=("true",), needs=("code",), required_env=(bad_env_name,))
    with pytest.raises(ValueError, match="must match"):
        RuntimeHooksManifest.from_dict(_manifest_dict(_row("a"), optional_env=[bad_env_name]))


def test_check_row_and_manifest_validation_errors() -> None:
    def code_row(**kw: object) -> CheckRow:
        return CheckRow(id="a", argv=("true",), needs=("code",), **kw)  # type: ignore[arg-type]

    for bad_timeout in (0, 3601, True):
        with pytest.raises(ValueError, match="timeout_seconds"):
            code_row(timeout_seconds=bad_timeout)
    with pytest.raises(ValueError, match="duplicate"):
        code_row(required_env=("FOO", "FOO"))
    with pytest.raises(ValueError, match="both required_env and optional_env"):
        code_row(required_env=("FOO",), optional_env=("FOO",))
    with pytest.raises(ValueError, match="unknown check row field"):
        CheckRow.from_dict({**_row("a"), "extra": 1})
    with pytest.raises(ValueError, match="argv"):
        CheckRow(id="a", argv=(), needs=("code",))

    base = _sample_manifest_dict()
    with pytest.raises(ValueError, match="unknown runtime hooks manifest field"):
        RuntimeHooksManifest.from_dict({**base, "unexpected": "x"})
    with pytest.raises(ValueError, match="unknown runtime hooks manifest field"):
        RuntimeHooksManifest.from_dict({**base, "slots": {}})
    with pytest.raises(ValueError, match="requires 'schema_version'"):
        RuntimeHooksManifest.from_dict({k: v for k, v in base.items() if k != "schema_version"})
    with pytest.raises(ValueError, match="must be a JSON object"):
        RuntimeHooksManifest.from_dict([])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="'checks' must be a JSON list"):
        RuntimeHooksManifest.from_dict({**base, "checks": {}})
    with pytest.raises(ValueError, match="non-empty 'checks' list"):
        RuntimeHooksManifest.from_dict({**base, "checks": []})
    with pytest.raises(ValueError, match="duplicate check id"):
        RuntimeHooksManifest.from_dict(_manifest_dict(_row("a"), _row("a")))
    with pytest.raises(ValueError, match="'service' must be a non-empty string"):
        RuntimeHooksManifest.from_dict({**base, "service": "  "})
    with pytest.raises(ValueError, match="'artifacts_dir' must be a non-empty string"):
        RuntimeHooksManifest.from_dict({**base, "artifacts_dir": ""})
    with pytest.raises(ValueError, match="must be a CheckRow"):
        RuntimeHooksManifest("s", checks=("not a row",))  # type: ignore[arg-type]


def test_check_row_checkout_lineage() -> None:
    default_row = CheckRow(id="a", argv=("true",), needs=("code",))
    assert default_row.checkout_lineage is False
    assert "checkout_lineage" not in default_row.to_dict()
    assert CheckRow.from_dict(default_row.to_dict()) == default_row

    explicit_row = CheckRow(id="a", argv=("true",), needs=("code",), checkout_lineage=True)
    assert explicit_row.to_dict()["checkout_lineage"] is True
    roundtripped = CheckRow.from_dict(explicit_row.to_dict())
    assert roundtripped == explicit_row and roundtripped.checkout_lineage is True

    for bad in (1, "true"):
        with pytest.raises(ValueError, match="'checkout_lineage' must be a bool"):
            CheckRow(id="a", argv=("true",), needs=("code",), checkout_lineage=bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="'checkout_lineage' must be a bool"):
            CheckRow.from_dict({**_row("a"), "checkout_lineage": bad})


def test_build_hook_env_strips_ambient_secrets_and_injects_canonical_vars() -> None:
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
        required_env=("APP_API_TOKEN",),
        optional_env=("OPTIONAL_FLAG", "EMPTY_OPTIONAL"),
        slot="preview_gate",
        app_url="https://pr-42.preview.example.com",
        environment="preview",
        version_ref="abc1234",
        artifacts_dir=".artifacts/custom",
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
    assert clean["ENVIRONMENT"] == "preview"
    assert clean["APP_VERSION_REF"] == "abc1234"
    assert clean["HOOK_SLOT"] == "preview_gate"
    assert clean["HOOK_ARTIFACTS_DIR"] == ".artifacts/custom"
    for removed in ("DEPLOY_BASE_URL", "STAGING_BASE_URL", "TA_BASE_URL", "EXPECTED_SHA"):
        assert removed not in clean, f"{removed} was removed in 4.0.0"


def test_build_hook_env_defaults_pythonpath_to_dot_and_allows_explicit_override() -> None:
    common = {
        "slot": "smoke",
        "app_url": "http://127.0.0.1:8000",
        "environment": "local_dev",
        "version_ref": "local",
        "artifacts_dir": ".artifacts/hooks",
    }
    clean_default = build_hook_env(
        **common, ambient_env={"PATH": "/usr/bin", "PYTHONPATH": "/ambient/ignored"}
    )
    assert clean_default["PYTHONPATH"] == "."

    clean_opt = build_hook_env(
        **common, optional_env=("PYTHONPATH",), ambient_env={"PYTHONPATH": "src:apps/web"}
    )
    assert clean_opt["PYTHONPATH"] == "src:apps/web"

    clean_req = build_hook_env(
        **common, required_env=("PYTHONPATH",), ambient_env={"PYTHONPATH": "custom/lib"}
    )
    assert clean_req["PYTHONPATH"] == "custom/lib"


def test_run_cell_missing_required_env_fails_closed(tmp_path: Path) -> None:
    manifest = RuntimeHooksManifest.from_dict(_sample_manifest_dict())
    result_file = tmp_path / "out" / "hook-result.json"
    calls: list[list[str]] = []

    def fake_runner(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    res = _run_preview_post(
        manifest, ambient_env={"PATH": "/usr/bin"}, result_path=result_file, runner=fake_runner
    )
    assert calls == []
    assert res.status == "fail"
    assert res.exit_code == 2
    assert res.failed_step == "env"
    assert "APP_API_TOKEN" in res.summary and "E2E_USER_EMAIL" in res.summary
    saved = json.loads(result_file.read_text(encoding="utf-8"))
    assert saved["status"] == "fail"
    assert saved["failed_step"] == "env"


def test_run_cell_bootstrap_failure_skips_run_command() -> None:
    manifest = RuntimeHooksManifest.from_dict(_sample_manifest_dict())
    calls: list[list[str]] = []

    def fake_runner(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 17, stdout="", stderr="playwright install failed")

    res = _run_preview_post(
        manifest,
        ambient_env={"E2E_USER_EMAIL": "bot@example.com", "APP_API_TOKEN": "t"},
        runner=fake_runner,
    )
    assert calls == [["sh", "-lc", "playwright install chromium"]]
    assert res.status == "fail"
    assert res.exit_code == 17
    assert res.failed_step == "bootstrap"
    assert "playwright install failed" in res.stderr_tail


def test_run_cell_timeout_records_timeout_step() -> None:
    manifest = _preview_post_manifest(_row("slow", timeout_seconds=60))

    def timeout_runner(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=60, stderr="hung on request")

    res = _run_preview_post(manifest, runner=timeout_runner)
    assert res.status == "fail"
    assert res.exit_code == 124
    assert res.failed_step == "timeout"
    assert "timed out after 60s" in res.summary
    assert "hung on request" in res.stderr_tail


def test_run_cell_preserves_both_stdout_and_stderr_when_both_non_empty() -> None:
    boot_manifest = _preview_post_manifest(_row("e2e", writes=True, bootstrap="install"))
    plain_manifest = _preview_post_manifest(_row("plain"))

    def dual_stream_runner(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout="Error: ERROR: Playwright does not support chromium on ubuntu26.04-x64\n",
            stderr="Installed 16 packages in 9ms\n",
        )

    res = _run_preview_post(boot_manifest, runner=dual_stream_runner)
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

    huge_res = _run_preview_post(plain_manifest, runner=huge_dual_stream_runner)
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

    timeout_res = _run_preview_post(plain_manifest, runner=dual_timeout_runner)
    assert timeout_res.failed_step == "timeout"
    assert "--- stdout ---" in timeout_res.stderr_tail
    assert "stdout before timeout" in timeout_res.stderr_tail
    assert "--- stderr ---" in timeout_res.stderr_tail
    assert "stderr before timeout" in timeout_res.stderr_tail


def test_cli_check_hooks_and_removed_run_hook_option(tmp_path: Path) -> None:
    manifest_path = tmp_path / "runtime-hooks.manifest.json"
    manifest_path.write_text(
        json.dumps(_sample_manifest_dict(service="truealpha/app"), indent=2), encoding="utf-8"
    )

    output: list[str] = []
    rc = main(["--check-hooks", "runtime-hooks.manifest.json"], root=tmp_path, echo=output.append)
    assert rc == 0
    assert output == ["runtime hooks manifest: ok (truealpha/app, v3, 2 checks)"]

    missing: list[str] = []
    assert main(["--check-hooks", "absent.json"], root=tmp_path, echo=missing.append) == 1
    assert "absent.json" in missing[0]

    # --run-hook was removed in 4.0.0; argparse refuses it with exit code 2.
    with pytest.raises(SystemExit) as exc:
        main(["--run-hook", "smoke", "--app-url", "http://127.0.0.1:8000"], root=tmp_path)
    assert exc.value.code == 2


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


def test_run_cell_runs_each_check_and_records_it(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[0] == "b":
            return subprocess.CompletedProcess(cmd, 3, "", "\nboom first\nsecond")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    kw = {"cwd": tmp_path, "runner": fake}
    file = tmp_path / ".artifacts/hooks/checks.jsonl"
    file.parent.mkdir(parents=True)
    file.write_text('{"source": "stale", "checks": [], "complete": true}\n')

    m = _preview_post_manifest(_row("a"), _row("b"), _row("c"))
    res = _run_preview_post(m, **kw)
    assert [c[0] for c in calls] == ["a", "b", "c"]  # preview.post runs every check
    assert (res.status, res.exit_code, res.failed_step) == ("fail", 3, "run")
    assert [(c.name, c.ok, c.detail) for c in res.checks] == [
        ("a", True, ""),
        ("b", False, "boom first"),
        ("c", True, ""),
    ]
    assert all(c.seconds >= 0 for c in res.checks) and isinstance(res.checks[0], CheckVerdict)
    assert len(file.read_text().splitlines()) == 1  # stale line deleted, one line written
    trail = load_trail(file, expected_sources=["preview_gate"])
    assert trail.is_green("a") and "b" in trail.failing

    calls.clear()
    res = _run_preview_post(_preview_post_manifest(_row("only", "echo", "ok")), **kw)
    assert res.status == "pass" and calls == [["echo", "ok"]]
    assert [(c.name, c.ok) for c in res.checks] == [("only", True)]
    assert load_trail(file, expected_sources=["preview_gate"]).is_green("only")
    assert "a" not in load_trail(file, expected_sources=["preview_gate"]).green


@pytest.mark.parametrize("exc", [FileNotFoundError, KeyboardInterrupt])
def test_run_cell_raising_runner_still_writes_the_check_file(
    tmp_path: Path, exc: type[BaseException]
) -> None:
    def boom(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "b":
            raise exc("no such binary: b")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    m = _preview_post_manifest(_row("a"), _row("b"))
    with pytest.raises(exc):
        _run_preview_post(m, cwd=tmp_path, runner=boom)
    trail = load_trail(
        tmp_path / ".artifacts/hooks/checks.jsonl", expected_sources=["preview_gate"]
    )
    assert trail.is_green("a")
    assert "no such binary" in trail.failing["b"].detail


def test_checks_of_one_cell_share_one_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    timeouts: list[float] = []

    def slow(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        timeouts.append(kw["timeout"])  # type: ignore[arg-type]
        now[0] += 12  # each check needs 12 s of the 30 s local.pre budget
        if kw["timeout"] < 12:  # type: ignore[operator]
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])  # type: ignore[arg-type]
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(
        "infra2_sdk.manifests.time", SimpleNamespace(monotonic=lambda: now[0]), raising=True
    )
    assert CELL_POLICIES["local.pre"].budget_seconds == 30
    m = _preview_post_manifest(
        *(_row(i, needs=["code"], timeout_seconds=20) for i in ("a", "b", "c"))
    )
    res = run_cell(m, "local", "pre", ambient_env={}, runner=slow)
    assert timeouts == pytest.approx([20.0, 18.0, 6.0])
    assert (res.status, res.exit_code, res.failed_step) == ("fail", 124, "timeout")
    assert [(c.name, c.ok) for c in res.checks] == [("a", True), ("b", True), ("c", False)]


def test_run_cell_detail_is_the_first_stderr_line_else_stdout() -> None:
    def make(out: str, err: str):
        return lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, out, err)

    m = _preview_post_manifest(_row("x"))
    both = _run_preview_post(m, runner=make("collected 10\n.F", "\nAssertionError: boom"))
    assert both.checks[0].detail == "AssertionError: boom"
    only_out = _run_preview_post(m, runner=make("only out\nmore", ""))
    assert only_out.checks[0].detail == "only out"


# --- G1 (Hook Grid policy table, schema 3, run_cell, skipped verdicts) --------------------------


_TODO_PROBE = 'python3 probe_health.py "${TARGET_URL:-$APP_URL}"'
_TODO_REQ = f"{_TODO_PROBE} --require-service platform/todo"
_TODO_GATE = f'{_TODO_REQ} --gate "$EXPECTED_SHA"'

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


_EXPECTED_CELL_PROJECTION: dict[str, dict[str, list[str]]] = {
    "platform_todo": {
        "preview.operation": ["smoke"],
        "staging.operation": ["smoke"],
        "prod.operation": ["smoke"],
        "preview.post": ["preview_gate"],
        "staging.post": ["staging_gate"],
        "prod.post": ["prod_gate"],
        "scheduled_audit": ["scheduled_audit"],
    },
    "finance_report": {
        "preview.operation": ["smoke"],
        "staging.operation": ["smoke"],
        "prod.operation": ["smoke"],
        "preview.post": ["smoke", "preview_minimum_e2e", "touched_files"],
        "staging.post": [
            "smoke",
            "staging_core_e2e",
            "ai_provider_connectivity",
            "staging_ai_ocr",
        ],
        "prod.post": ["prod_gate"],
        "scheduled_audit": [
            "audit_replay",
            "core_journey",
            "pdf_corpus_oracle",
            "financial_scenario_benchmark",
        ],
    },
    "truealpha": {
        "preview.operation": ["smoke"],
        "staging.operation": ["smoke"],
        "prod.operation": ["smoke"],
        "preview.post": ["health", "warm_surface", "walk_tree"],
        "staging.post": ["health", "warm_surface", "walk_tree"],
        "prod.post": ["prod_gate"],
        "scheduled_audit": ["nightly_verdicts"],
    },
}


@pytest.mark.parametrize(
    ("name", "v3_dict"),
    [
        ("platform_todo", _TODO_V3),
        ("finance_report", _FINANCE_REPORT_V3),
        ("truealpha", _TRUEALPHA_V3),
    ],
)
def test_g1_ac1_schema_3_manifests_keep_their_cell_projection(
    name: str, v3_dict: dict[str, object]
) -> None:
    m3 = RuntimeHooksManifest.from_dict(v3_dict)
    assert m3.schema_version == 3
    assert RuntimeHooksManifest.from_dict(m3.to_dict()) == m3
    assert _cell_projection(m3) == _EXPECTED_CELL_PROJECTION[name]

    # Mutation check: removing one cell from one row must change the projection.
    mutated = json.loads(json.dumps(v3_dict))
    row0_cells = mutated["checks"][0].setdefault("cells", {})
    if "preview.post" in row0_cells.get("add", {}):
        del row0_cells["add"]["preview.post"]
    else:
        row0_cells.setdefault("remove", {})["preview.operation"] = {
            "reason": "mutation",
            "issue": "test#1",
        }
    assert (
        _cell_projection(RuntimeHooksManifest.from_dict(mutated)) != _EXPECTED_CELL_PROJECTION[name]
    )


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

    # The policy table is read from stages.yaml, not hardcoded literals.
    yaml_copy = tmp_path / "stages.yaml"
    base_yaml = (
        Path(__file__).resolve().parents[1] / "src" / "infra2_sdk" / "data" / "stages.yaml"
    ).read_text(encoding="utf-8")
    assert CELL_POLICIES["preview.post"].budget_seconds == 360
    yaml_copy.write_text(
        base_yaml.replace("budget_seconds: 360\n", "budget_seconds: 444\n"), encoding="utf-8"
    )
    pv, policies = _load_policy_table(yaml_copy)
    assert pv == 1
    assert policies["preview.post"].budget_seconds == 444
    assert _derive_stage_plans(policies)["preview"].stage_id == "preview.runtime"

    # format_check_grid prints checks x cells for a schema 3 manifest.
    m3 = RuntimeHooksManifest.from_dict(_TRUEALPHA_V3)
    grid3 = format_check_grid(m3)
    assert "Service: truealpha/app (schema v3, policy v1)" in grid3
    assert "`health`" in grid3 and "`nightly_verdicts`" in grid3
    assert "preview.post" in grid3 and "local.pre" in grid3


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
