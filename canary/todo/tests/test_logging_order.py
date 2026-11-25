"""Logging order test for platform/30.todo (#1213, #1209).

`init_service(..., enable_otel=True)` runs at import time. With a real OTLP endpoint it adds
an OpenTelemetry `LoggingHandler` to the root logger. `logging.basicConfig` does nothing when
the root logger already has a handler. `init_service` calls `basicConfig` at INFO before it
attaches that handler, so the app holds no `basicConfig` call of its own. If the order broke,
the root level would stay WARNING and every app INFO line would be lost.

Root logger state is global, so each case loads the app in a fresh subprocess.
This test needs the real OpenTelemetry packages from the `dev` dependency group.
A missing package makes the subprocess exit with an error. The test then fails.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from conftest import TODO_APP, clear_telemetry_env

# The script imports the app, stubs the call that starts the real server, and runs main().
# It prints one JSON line with the root logger state, then exits without atexit hooks.
# The OTLP exporter points at a closed port, so a normal exit would wait on the flush.
_PROBE_SCRIPT = textwrap.dedent(
    """
    import importlib.util
    import json
    import logging
    import os
    import sys
    import types

    # The names that the shell must not hand to the child (#1238).
    inherited = sorted(
        name for name in ("APP_ENV", "IMAGE_TAG", "SERVICE_VERSION") if name in os.environ
    )

    uvicorn_stub = types.ModuleType("uvicorn")
    uvicorn_stub.run = lambda *args, **kwargs: None
    sys.modules["uvicorn"] = uvicorn_stub

    spec = importlib.util.spec_from_file_location("todo_app_logging_order", sys.argv[1])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()

    root = logging.getLogger()
    state = {
        "level": root.level,
        "handlers": [
            type(h).__module__ + "." + type(h).__qualname__ for h in root.handlers
        ],
        "inherited": inherited,
    }
    sys.stdout.write(json.dumps(state) + "\\n")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
    """
)


def _run_probe(app_path: Path) -> tuple[dict, str]:
    # The child gets the shell environment without the SDK variables and aliases (#1238).
    with pytest.MonkeyPatch.context() as patch:
        clear_telemetry_env(patch)
        env = dict(os.environ)
    env["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://127.0.0.1:1"
    env["OTEL_SERVICE_NAME"] = "platform-todo"
    env["OTEL_RESOURCE_ATTRIBUTES"] = (
        "deployment.environment.name=staging,infra.service.id=platform/todo,"
        f"service.version={'a' * 40},infra.iac.ref={'a' * 40}"
    )
    env["ENVIRONMENT"] = "staging"
    env["DATABASE_URL"] = "postgresql://todo_app:todo_test_password_0123456789@127.0.0.1:1/postgres"
    env["PORT"] = "8000"
    result = subprocess.run(
        [sys.executable, "-c", _PROBE_SCRIPT, str(app_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        f"probe exit code {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    lines = [json.loads(ln) for ln in result.stdout.splitlines() if ln.startswith("{")]
    states = [item for item in lines if "handlers" in item]
    logs = [item for item in lines if "logger" in item]
    assert len(states) == 1, f"expected one state JSON line, got stdout:\n{result.stdout}"
    return (states[0], logs), result.stderr


def test_todo_main_leaves_root_logger_at_info_with_otel_handler() -> None:
    """After `main()`, the root level is INFO and both handlers are present."""
    (state, logs), _stderr = _run_probe(TODO_APP)

    assert state["level"] == logging.INFO, (
        f"root level is {state['level']}, expected {logging.INFO}; handlers: {state['handlers']}"
    )
    assert state["handlers"].count("logging.StreamHandler") == 1, state["handlers"]
    otel_handlers = [
        h
        for h in state["handlers"]
        if h.startswith("opentelemetry.") and h.endswith(".LoggingHandler")
    ]
    assert len(otel_handlers) == 1, state["handlers"]
    # The stream handler writes structured JSON to stdout. A dropped INFO line fails here.
    assert any(
        entry.get("level") == "INFO"
        and entry.get("logger") == "platform-todo"
        and "Canary Todo service starting on port 8000" in entry.get("message", "")
        and entry.get("service.name") == "platform-todo"
        for entry in logs
    ), logs


def test_the_probe_child_drops_the_contract_and_ignores_the_retired_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The child must not inherit `SERVICE_VERSION` of the shell (#1238). `IMAGE_TAG` and
    `APP_ENV` are retired names (infra2#1823 N1). The child inherits them, but the SDK does
    not read them, so a value that differs from `SERVICE_VERSION` or the tier cannot stop
    the strict start."""
    monkeypatch.setenv("SERVICE_VERSION", "1.0.0")
    monkeypatch.setenv("IMAGE_TAG", "2.0.0")
    monkeypatch.setenv("APP_ENV", "production")

    (state, _logs), _stderr = _run_probe(TODO_APP)

    assert state["inherited"] == ["APP_ENV", "IMAGE_TAG"], state
    assert state["level"] == logging.INFO, state
