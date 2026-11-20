"""Probe log noise test for platform/30.todo (#1436).

In production the app exports logs. `init_service` sets the root logger to INFO when the OTLP
endpoint is active. The canary probes call `httpx`, and `httpx` logs one INFO line for each
successful request. Measured over 24 h in SigNoz, those lines were 75% of the exported
`platform-todo` log lines, and the canary result line was the other 25%.

The app sets the `httpx` and `httpcore` loggers to WARNING. A successful probe call then logs
nothing from those libraries, a failed probe call still logs through the service, and the
canary result line stays.

Logger levels are global state, so each scenario loads the app in a fresh subprocess with the
same telemetry environment as production (an active OTLP endpoint, which puts the root logger
at INFO). The OTLP endpoint is a closed port, because the test needs only the log records.
A capturing handler sits on the root logger, at the place where the OpenTelemetry handler
sits, so it sees every record that the OpenTelemetry handler would export.

The probes call a real HTTP server on 127.0.0.1 and a closed local port. The test fakes only
the three gating probes (Postgres, Redis, S3), because they need other services and they do
not call `httpx`. The server counts the requests it receives, so a probe that never ran
cannot pass the test.
"""

from __future__ import annotations

import http.server
import json
import logging
import os
import socket
import subprocess
import sys
import textwrap
import threading
from collections import Counter
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import TODO_APP, clear_telemetry_env

LIBRARY_LOGGERS = ("httpx", "httpcore")
HTTPX_MARKER = "probe-marker httpx warning"
HTTPCORE_MARKER = "probe-marker httpcore error"
HTTPCORE_INFO_MARKER = "probe-marker httpcore info"

# The script loads the app, replaces the three gating probes, and calls the status route of
# the real FastAPI app. The route runs the real `_probe_http` for the three informational
# checks. It then emits one INFO line through a `httpcore` child logger (`httpcore` itself logs
# below INFO, so a real call cannot show its level) and one WARNING and one ERROR through the
# library loggers, to show that the higher levels still pass. It prints one JSON line and exits
# without atexit hooks,
# because the OTLP exporter points at a closed port and a normal exit would wait on the flush.
_PROBE_SCRIPT = textwrap.dedent(
    """
    import importlib.util
    import json
    import logging
    import os
    import sys

    from starlette.testclient import TestClient

    spec = importlib.util.spec_from_file_location("todo_app_probe_noise", sys.argv[1])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


    class Capture(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.NOTSET)
            self.rows = []

        def emit(self, record):
            self.rows.append([record.name, record.levelno, record.getMessage()])


    capture = Capture()
    logging.getLogger().addHandler(capture)

    passed = {"status": "pass", "latency_ms": 0.0, "detail": "fake gating probe"}
    module._probe_postgres = lambda *args, **kwargs: dict(passed)
    module._probe_redis = lambda *args, **kwargs: dict(passed)
    module._probe_s3 = lambda *args, **kwargs: dict(passed)

    response = TestClient(module.fastapi_app).get("/api/canary/status")

    logging.getLogger("httpcore.http11").info(sys.argv[4])
    logging.getLogger("httpx").warning(sys.argv[2])
    logging.getLogger("httpcore.connection").error(sys.argv[3])

    state = {
        "status_code": response.status_code,
        "checks": {
            name: check["status"] for name, check in response.json()["checks"].items()
        },
        "root_level": logging.getLogger().level,
        "records": capture.rows,
    }
    sys.stdout.write(json.dumps(state) + "\\n")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
    """
)


class _Handler(http.server.BaseHTTPRequestHandler):
    """`/ok` answers 200 and `/down` answers 503. The server counts each request."""

    def do_GET(self) -> None:  # noqa: N802 - the name is fixed by BaseHTTPRequestHandler
        self.server.hits[self.path] += 1  # type: ignore[attr-defined]
        self.send_response(200 if self.path == "/ok" else 503)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Keep the test output free of server access lines."""


@pytest.fixture(scope="module")
def local_server() -> Iterator[http.server.ThreadingHTTPServer]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.hits = Counter()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _closed_local_port() -> int:
    """Return a local port with no listener, so a connection to it is refused."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _run_scenario(*, signoz: str, openpanel: str, authentik: str) -> tuple[dict[str, Any], str]:
    with pytest.MonkeyPatch.context() as patch:
        clear_telemetry_env(patch)
        env = dict(os.environ)
    # An active endpoint makes `init_service` set the root logger to INFO, as in production.
    env["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://127.0.0.1:1"
    env["OTEL_SERVICE_NAME"] = "platform-todo"
    env["OTEL_RESOURCE_ATTRIBUTES"] = (
        "deployment.environment.name=staging,infra.service.id=platform/todo,"
        f"service.version={'a' * 40},infra.iac.ref={'a' * 40}"
    )
    env["SIGNOZ_OTEL_COLLECTOR"] = signoz
    env["OPENPANEL_API_URL"] = openpanel
    env["AUTHENTIK_ENDPOINT"] = authentik
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _PROBE_SCRIPT,
            str(TODO_APP),
            HTTPX_MARKER,
            HTTPCORE_MARKER,
            HTTPCORE_INFO_MARKER,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        f"probe exit code {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    states = [
        json.loads(ln)
        for ln in result.stdout.splitlines()
        if ln.startswith("{") and '"records"' in ln
    ]
    assert len(states) == 1, f"expected one state JSON line, got stdout:\n{result.stdout}"
    return states[0], result.stderr


def _library_rows(state: dict[str, Any]) -> list[list[Any]]:
    """The records of `httpx` and `httpcore` (and their children), without the WARNING and
    ERROR markers. The INFO marker stays: it must not appear."""
    return [
        row
        for row in state["records"]
        if row[0].split(".")[0] in LIBRARY_LOGGERS and row[2] not in (HTTPX_MARKER, HTTPCORE_MARKER)
    ]


@pytest.fixture(scope="module")
def all_probes_ok(local_server: http.server.ThreadingHTTPServer) -> dict[str, Any]:
    base = f"http://127.0.0.1:{local_server.server_port}"
    local_server.hits.clear()
    state, _stderr = _run_scenario(
        signoz=f"{base}/ok", openpanel=f"{base}/ok", authentik=f"{base}/ok"
    )
    state["server_hits"] = dict(local_server.hits)
    return state


@pytest.fixture(scope="module")
def some_probes_down(local_server: http.server.ThreadingHTTPServer) -> dict[str, Any]:
    base = f"http://127.0.0.1:{local_server.server_port}"
    local_server.hits.clear()
    state, _stderr = _run_scenario(
        signoz=f"{base}/ok",
        openpanel=f"{base}/down",
        authentik=f"http://127.0.0.1:{_closed_local_port()}/-/health/live/",
    )
    state["server_hits"] = dict(local_server.hits)
    return state


def test_successful_probe_calls_log_nothing_from_httpx(
    all_probes_ok: dict[str, Any],
) -> None:
    """Three successful `httpx` calls leave no record of `httpx` or `httpcore`, and the
    canary result line stays (#1436)."""
    state = all_probes_ok

    # The probes ran for real: the server saw the three calls and the document says so.
    assert state["server_hits"] == {"/ok": 3}, state["server_hits"]
    assert state["status_code"] == 200, state
    assert {state["checks"][n] for n in ("signoz", "openpanel", "authentik")} == {"pass"}, state[
        "checks"
    ]
    # The app runs in the production log state: the root logger sits at INFO.
    assert state["root_level"] == logging.INFO, state["root_level"]

    assert _library_rows(state) == [], (
        f"httpx or httpcore logged during a successful probe run: {_library_rows(state)}"
    )
    # The canary result line is the positive proof of a run, so it must remain.
    assert ["platform-todo", logging.INFO, "canary run ok: 3 write paths passed"] in (
        state["records"]
    ), state["records"]


def test_failed_probe_calls_still_log_a_warning(
    some_probes_down: dict[str, Any],
) -> None:
    """A probe that gets HTTP 503 and a probe that gets a refused connection each still
    log a WARNING that names the check and the reason. The `httpx` INFO line for the
    503 response goes, because the service line carries the same status (#1436)."""
    state = some_probes_down

    assert state["server_hits"] == {"/ok": 1, "/down": 1}, state["server_hits"]
    assert state["checks"]["signoz"] == "pass", state["checks"]
    assert state["checks"]["openpanel"] == "fail", state["checks"]
    assert state["checks"]["authentik"] == "fail", state["checks"]

    assert _library_rows(state) == [], _library_rows(state)

    warnings = [
        (name, message)
        for name, level, message in state["records"]
        if name == "platform-todo" and level >= logging.WARNING
    ]
    assert ("platform-todo", "canary check openpanel is fail: HTTP 503") in warnings, warnings
    refused = [
        message
        for _name, message in warnings
        if message.startswith("canary check authentik is fail: ConnectError")
    ]
    assert len(refused) == 1, warnings
    assert (
        "platform-todo",
        "canary informational checks not passing: openpanel,authentik",
    ) in warnings, warnings
    # The gating probes passed, so the canary result line stays INFO and positive.
    assert ["platform-todo", logging.INFO, "canary run ok: 3 write paths passed"] in (
        state["records"]
    ), state["records"]


def test_httpx_and_httpcore_warning_and_error_records_still_pass(
    some_probes_down: dict[str, Any],
) -> None:
    """The fix raises the library loggers to WARNING. It must not hide a WARNING or an
    ERROR from them, including from a child logger such as `httpcore.connection`."""
    records = [tuple(row) for row in some_probes_down["records"]]

    assert ("httpx", logging.WARNING, HTTPX_MARKER) in records, records
    assert ("httpcore.connection", logging.ERROR, HTTPCORE_MARKER) in records, records
