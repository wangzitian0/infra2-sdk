"""Tests for stdlib log-context propagation across stdout JSON logs and OTLP LogRecords."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from types import SimpleNamespace

import pytest
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor

from infra2_sdk.runtime.llm import llm_call_context
from infra2_sdk.runtime.otel import (
    _LOG_CONTEXT,
    _bind_log_context,
    _JsonLogFormatter,
    _logging_handler_class,
    _RedactingLogRecordProcessor,
)


def test_stdlib_log_context_propagates_to_json_and_otlp_without_structlog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, "structlog", raising=False)

    formatter = _JsonLogFormatter(
        {
            "service.name": "todo-canary",
            "deployment.environment.name": "staging",
            "service.version": "3.34.0",
        }
    )
    exporter = InMemoryLogRecordExporter()
    lp = LoggerProvider()
    lp.add_log_record_processor(_RedactingLogRecordProcessor())
    lp.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    otel_handler = _logging_handler_class()(level=logging.NOTSET, logger_provider=lp)

    logger = logging.getLogger("infra2.test.otel.logctx")
    logger.setLevel(logging.INFO)
    logger.handlers = [otel_handler]
    logger.propagate = False

    try:
        with llm_call_context(
            call_site="todo.summary",
            decoding_version="v2",
            domain_run_id="run-42",
        ):
            rec = logging.LogRecord(
                "infra2.test.otel.logctx",
                logging.INFO,
                __file__,
                99,
                "token=sk-secret99999 user alice@example.com",
                (),
                None,
            )
            formatted = json.loads(formatter.format(rec))
            logger.info("hello sk-secret99999 bob@example.com")

        assert formatted["infra.llm.call_site"] == "todo.summary"
        assert formatted["infra.llm.decoding_version"] == "v2"
        assert formatted["infra.llm.domain_run_id"] == "run-42"
        assert formatted["code.file.path"] == "tests/test_otel.py"
        assert formatted["code.function.name"] == (
            "test_stdlib_log_context_propagates_to_json_and_otlp_without_structlog"
        )
        assert isinstance(formatted["code.line.number"], int) and formatted["code.line.number"] > 0
        assert "sk-secret99999" not in formatted["message"]
        assert "alice@example.com" not in formatted["message"]

        lp.force_flush()
        exported = exporter.get_finished_logs()
        assert len(exported) == 1
        attrs = dict(exported[0].log_record.attributes or {})
        assert attrs["infra.llm.call_site"] == "todo.summary"
        assert attrs["infra.llm.decoding_version"] == "v2"
        assert attrs["infra.llm.domain_run_id"] == "run-42"
        assert attrs["code.file.path"] == formatted["code.file.path"]
        assert attrs["code.function.name"] == formatted["code.function.name"]
        assert attrs["code.line.number"] == formatted["code.line.number"]
        body = str(exported[0].log_record.body)
        assert "sk-secret99999" not in body
        assert "bob@example.com" not in body

        assert not _LOG_CONTEXT.get()
    finally:
        logger.handlers = []
        lp.shutdown()


def test_bind_log_context_mirrors_structlog_when_present_and_restores_on_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store: dict[str, object] = {"existing": "outer"}

    class _FakeCtxVars:
        @staticmethod
        def get_contextvars() -> dict[str, object]:
            return dict(store)

        @staticmethod
        def bind_contextvars(**kwargs: object) -> None:
            store.update(kwargs)

        @staticmethod
        def unbind_contextvars(*keys: str) -> None:
            for k in keys:
                store.pop(k, None)

    monkeypatch.setitem(sys.modules, "structlog", SimpleNamespace(contextvars=_FakeCtxVars()))
    with _bind_log_context({"infra.llm.call_site": "inner.site", "existing": "inner"}):
        assert store["infra.llm.call_site"] == "inner.site"
        assert store["existing"] == "inner"
    assert store == {"existing": "outer"}
    assert not _LOG_CONTEXT.get()


def test_bind_log_context_exits_cleanly_in_copied_context() -> None:
    cm = _bind_log_context({"infra.llm.call_site": "copied.ctx"})
    cm.__enter__()
    assert (_LOG_CONTEXT.get() or {})["infra.llm.call_site"] == "copied.ctx"
    copied = contextvars.copy_context()
    copied.run(cm.__exit__, None, None, None)
    assert not copied.run(_LOG_CONTEXT.get)
    _LOG_CONTEXT.set(None)
