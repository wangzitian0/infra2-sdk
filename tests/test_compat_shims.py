"""Tests verifying backwards-compatible shims re-export canonical capabilities."""

from __future__ import annotations

import json
from types import ModuleType

import infra2_sdk.deploy as deploy_mod
import infra2_sdk.deploy_health as deploy_health_shim
import infra2_sdk.dispatch as dispatch_shim
import infra2_sdk.refs as refs_mod
import infra2_sdk.release as release_shim
import infra2_sdk.runtime.dependencies as dependencies_shim
import infra2_sdk.runtime.environ as environ_shim
import infra2_sdk.runtime.environment as environment_mod
import infra2_sdk.runtime.health as health_mod
import infra2_sdk.runtime.probes as probes_shim


def test_dispatch_shim_re_exports() -> None:
    assert isinstance(dispatch_shim, ModuleType)
    for name in dispatch_shim.__all__:
        assert hasattr(dispatch_shim, name), f"dispatch shim missing {name}"
        assert getattr(dispatch_shim, name) is getattr(deploy_mod, name) or callable(
            getattr(dispatch_shim, name)
        )


def test_deploy_health_shim_re_exports() -> None:
    assert isinstance(deploy_health_shim, ModuleType)
    for name in deploy_health_shim.__all__:
        assert hasattr(deploy_health_shim, name), f"deploy_health shim missing {name}"
        assert getattr(deploy_health_shim, name) is getattr(deploy_mod, name) or callable(
            getattr(deploy_health_shim, name)
        )


def test_release_shim_re_exports() -> None:
    assert isinstance(release_shim, ModuleType)
    for name in release_shim.__all__:
        assert hasattr(release_shim, name), f"release shim missing {name}"
        assert getattr(release_shim, name) is getattr(refs_mod, name)


def test_environ_shim_re_exports() -> None:
    assert isinstance(environ_shim, ModuleType)
    for name in environ_shim.__all__:
        assert hasattr(environ_shim, name), f"environ shim missing {name}"
        assert getattr(environ_shim, name) is getattr(environment_mod, name)


def test_dependencies_shim_re_exports() -> None:
    assert isinstance(dependencies_shim, ModuleType)
    for name in dependencies_shim.__all__:
        assert hasattr(dependencies_shim, name), f"dependencies shim missing {name}"
        assert getattr(dependencies_shim, name) is getattr(health_mod, name)


def test_probes_shim_re_exports() -> None:
    assert isinstance(probes_shim, ModuleType)
    for name in probes_shim.__all__:
        assert hasattr(probes_shim, name), f"probes shim missing {name}"
        assert getattr(probes_shim, name) is getattr(health_mod, name)


def test_dispatch_shim_main_cli(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("INFRA2_PAT", "fake-token")
    req = deploy_mod.DeployRequest(
        request_id="test-req-12345678",
        operation=deploy_mod.DeployOperation.DEPLOY,
        service="finance_report/app",
        deploy_type=deploy_mod.DeployType.STAGING,
        version_ref="v1.0.0",
        source_repository="wangzitian0/finance_report",
        source_sha="a" * 40,
        evidence=deploy_mod.DeployEvidence(
            source_run_url="https://github.com/wangzitian0/finance_report/actions/runs/1"
        ),
    )
    req_file = tmp_path / "req.json"
    req_file.write_text(json.dumps(req.to_dict()), encoding="utf-8")

    fake_run = deploy_mod.ReceiverRun(
        run_id=999,
        url="https://github.com/wangzitian0/infra2/actions/runs/999",
    )
    monkeypatch.setattr(
        "infra2_sdk.dispatch.github_api_client",
        lambda **kwargs: (lambda m, p, b: None, lambda r: b""),
    )
    monkeypatch.setattr(
        "infra2_sdk.dispatch.dispatch_and_wait",
        lambda *args, **kwargs: fake_run,
    )
    rc = dispatch_shim.main(["--request", str(req_file)])
    assert rc == 0


def test_deploy_health_shim_main_cli(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "infra2_sdk.deploy_health.default_http_get",
        lambda timeout: lambda url: (200, json.dumps({"status": "ok", "version": "v1.0.0"})),
    )
    rc = deploy_health_shim.main(
        [
            "https://example.test/health",
            "--expected-version",
            "v1.0.0",
            "--require-status",
            "ok",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "[OK] Health check passed" in captured.out
