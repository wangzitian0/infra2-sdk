"""Dependency health probes, FastAPI bootstrap, httpx client helpers, and rate limiting."""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from contextvars import copy_context
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from functools import partial
from threading import Thread
from typing import Any, Protocol, runtime_checkable
from weakref import WeakKeyDictionary

from pydantic import ConfigDict, Field, field_serializer, field_validator, model_validator

import infra2_sdk.telemetry as _telemetry
from infra2_sdk import __version__
from infra2_sdk._core import ContractModel, require_contract_version, require_optional
from infra2_sdk.config import (
    PLATFORM_OWNED_TIERS,
    EnvironmentTier,
    RuntimeEnvKey,
    read_runtime_env,
    resolve_environment,
    resolve_tier,
)
from infra2_sdk.telemetry import (
    OtelSettings,
    bind_request_id,
    configure_logging,
)

_DEPENDENCY_MANIFEST_VERSION = 1
_HTTP_OK, _HTTP_UNAVAILABLE = 200, 503
_DEFAULT_RETRY_STATUSES: frozenset[int] = frozenset({429, 502, 503, 504})
_LOGGER = logging.getLogger("infra2_sdk.service")
_INITIALIZED_ATTR = "infra2_service_name"


class DependencyKind(StrEnum):
    CODE_DOMINANT = "code_dominant"
    MODEL_DOMINANT = "model_dominant"


class DependencyStatus(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    SKIPPED = "skipped"


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class Dependency(ContractModel):
    name: str
    kind: DependencyKind
    required_in: frozenset[EnvironmentTier]
    env_vars: frozenset[str] = frozenset()
    summary: str = ""

    @field_validator("required_in", mode="before")
    @classmethod
    def _coerce_tiers(cls, value: Any) -> frozenset[EnvironmentTier]:
        return frozenset(resolve_tier(v) for v in value)

    @model_validator(mode="after")
    def _validate_dependency(self) -> Dependency:
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ValueError("dependency name must be a non-empty identifier")
        if not self.required_in:
            raise ValueError(f"dependency {self.name!r} must be required in at least one tier")
        if not self.env_vars:
            raise ValueError(f"dependency {self.name!r} must declare environment variables")
        if any(not k or k.upper() != k for k in self.env_vars):
            raise ValueError("dependency environment variables must be uppercase")
        return self

    @field_serializer("required_in")
    def _ser_required_in(self, value: frozenset[EnvironmentTier]) -> list[str]:
        return sorted(t.value for t in value)

    @field_serializer("env_vars")
    def _ser_env_vars(self, value: frozenset[str]) -> list[str]:
        return sorted(value)


class DependencyManifest(ContractModel):
    dependencies: tuple[Dependency, ...]
    contract_version: int = _DEPENDENCY_MANIFEST_VERSION

    @field_validator("contract_version", mode="before")
    @classmethod
    def _check_version(cls, value: Any) -> int:
        return require_contract_version(
            value, _DEPENDENCY_MANIFEST_VERSION, label="dependency manifest version"
        )

    @model_validator(mode="after")
    def _unique_names(self) -> DependencyManifest:
        if len(self.dependencies) != len({d.name for d in self.dependencies}):
            raise ValueError("duplicate dependency name")
        return self

    def __iter__(self):  # type: ignore[override]
        return iter(self.dependencies)

    def __len__(self) -> int:
        return len(self.dependencies)

    def __contains__(self, name: object) -> bool:
        return any(d.name == name for d in self.dependencies)

    def __getitem__(self, name: str) -> Dependency:
        if (dep := self.get(name)) is None:
            raise KeyError(name)
        return dep

    def get(self, name: str) -> Dependency | None:
        return next((d for d in self.dependencies if d.name == name), None)

    def names(self) -> frozenset[str]:
        return frozenset(d.name for d in self.dependencies)

    def required_for(self, tier: str | EnvironmentTier) -> frozenset[str]:
        resolved = resolve_tier(tier)
        return frozenset(d.name for d in self.dependencies if resolved in d.required_in)


class ProbeResult(ContractModel):
    name: str
    status: DependencyStatus
    detail: str = ""
    duration_ms: float = 0.0

    @model_validator(mode="after")
    def _validate_probe(self) -> ProbeResult:
        if not self.name:
            raise ValueError("probe name is required")
        if self.duration_ms < 0:
            raise ValueError("duration_ms must be non-negative")
        return self

    @property
    def present(self) -> bool:
        return self.status is DependencyStatus.PRESENT


@runtime_checkable
class DependencyCheck(Protocol):
    name: str

    def probe(self) -> ProbeResult | Awaitable[ProbeResult]: ...


CheckOutcome = ProbeResult | tuple[bool, str] | bool


class CallableCheck:
    """Wrap any sync or async callable as a DependencyCheck."""

    def __init__(
        self,
        name: str,
        fn: Callable[[], CheckOutcome | Awaitable[CheckOutcome]],
    ) -> None:
        self.name = name
        self.fn = fn

    def probe(self) -> ProbeResult | Awaitable[ProbeResult]:
        started = time.perf_counter()
        outcome = self.fn()
        if inspect.isawaitable(outcome):
            return self._finish(outcome, started)
        return self._coerce(outcome, started)

    async def _finish(self, outcome: Awaitable[CheckOutcome], started: float) -> ProbeResult:
        return self._coerce(await outcome, started)

    def _coerce(self, outcome: CheckOutcome, started: float) -> ProbeResult:
        elapsed_ms = (time.perf_counter() - started) * 1000
        if isinstance(outcome, ProbeResult):
            return outcome
        ok, detail = outcome if isinstance(outcome, tuple) else (bool(outcome), "")
        status = DependencyStatus.PRESENT if ok else DependencyStatus.ABSENT
        return ProbeResult(
            name=self.name,
            status=status,
            detail=str(detail or ("ok" if ok else "failed")),
            duration_ms=elapsed_ms,
        )


_MAX_CONCURRENT_SYNC_PROBES = 16
_SYNC_SEMAPHORES: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    WeakKeyDictionary()
)


def _sync_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if (sem := _SYNC_SEMAPHORES.get(loop)) is None:
        sem = _SYNC_SEMAPHORES[loop] = asyncio.Semaphore(_MAX_CONCURRENT_SYNC_PROBES)
    return sem


async def _run_sync_probe(probe: Callable[[], Any], *, name: str) -> Any:
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    ctx = copy_context()

    def publish(*, result: Any = None, error: BaseException | None = None) -> None:
        if not fut.done():
            fut.set_exception(error) if error is not None else fut.set_result(result)

    def worker() -> None:
        try:
            cb = partial(publish, result=ctx.run(probe))
        except BaseException as exc:  # noqa: BLE001
            cb = partial(publish, error=exc)
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(cb)

    async with _sync_semaphore():
        Thread(target=worker, name=f"infra2-probe-{name}", daemon=True).start()
        try:
            return await fut
        finally:
            if not fut.done():
                fut.cancel()


async def run_probes(
    checks: Sequence[DependencyCheck], *, timeout_seconds: float = 10.0
) -> tuple[ProbeResult, ...]:
    """Run dependency probes concurrently with bounded per-check timeouts."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    vals = tuple(checks)
    if len(vals) != len({c.name for c in vals}):
        raise ValueError("duplicate probe name")

    async def execute(check: DependencyCheck) -> ProbeResult:
        started = time.perf_counter()
        try:
            res = (
                await check.probe()
                if inspect.iscoroutinefunction(check.probe)
                else await _run_sync_probe(check.probe, name=check.name)
            )
            if inspect.isawaitable(res):
                res = await res
            if not isinstance(res, ProbeResult):
                raise TypeError("probe must return ProbeResult")
            if res.name != check.name:
                raise ValueError("probe result name does not match check name")
            return res
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(
                name=check.name,
                status=DependencyStatus.ABSENT,
                detail=f"{type(exc).__name__}: {exc}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )

    async def bounded(check: DependencyCheck) -> ProbeResult:
        try:
            return await asyncio.wait_for(execute(check), timeout=timeout_seconds)
        except TimeoutError:
            return ProbeResult(
                name=check.name,
                status=DependencyStatus.ABSENT,
                detail=f"probe timed out after {timeout_seconds:g}s",
                duration_ms=timeout_seconds * 1000,
            )

    return tuple(await asyncio.gather(*(bounded(c) for c in vals)))


async def evaluate_health(
    checks: Sequence[DependencyCheck],
    *,
    manifest: DependencyManifest | None = None,
    required: Collection[str] | None = None,
    tier: str | EnvironmentTier | None = None,
    timeout_seconds: float = 10.0,
) -> tuple[int, dict[str, Any]]:
    """Probe checks concurrently and evaluate HTTP 200 / 503 readiness body."""
    vals = tuple(checks)
    if manifest is not None:
        if required is not None:
            raise ValueError("pass either required or manifest, not both")
        if tier is None:
            raise ValueError("tier is required with a dependency manifest")
        req = manifest.required_for(tier)
    else:
        req = frozenset(required) if required is not None else frozenset(c.name for c in vals)

    results = await run_probes(vals, timeout_seconds=timeout_seconds)
    names = [r.name for r in results]
    if not results and not req:
        raise ValueError("no probe results and no required dependencies: nothing to report on")
    entry_map: dict[str, dict[str, Any]] = {
        r.name: {k: v for k, v in r.to_dict().items() if k != "name"} | {"required": r.name in req}
        for r in results
    }
    for n in sorted(req - set(names)):
        entry_map[n] = {
            "status": DependencyStatus.ABSENT.value,
            "detail": "required dependency was not probed",
            "duration_ms": 0.0,
            "required": True,
        }
    absent = sorted(
        (n for n, e in entry_map.items() if e["status"] != DependencyStatus.PRESENT.value),
        key=lambda n: (not entry_map[n]["required"], n),
    )
    status = (
        HealthStatus.UNHEALTHY
        if any(entry_map[n]["required"] for n in absent)
        else HealthStatus.DEGRADED
        if absent
        else HealthStatus.HEALTHY
    )
    body = {
        "status": status.value,
        "checks": dict(sorted(entry_map.items())),
        "reasons": [f"{n}: {entry_map[n]['detail'] or 'absent'}" for n in absent],
    }
    return (_HTTP_UNAVAILABLE if status is HealthStatus.UNHEALTHY else _HTTP_OK), body


class ProbeResponse(ContractModel):
    status: str
    service: str
    reasons: list[str] | None = None


class _CheckDetail(ContractModel):
    status: str
    detail: str
    duration_ms: float
    required: bool


class HealthResponse(ContractModel):
    model_config = ConfigDict(frozen=True, extra="allow", populate_by_name=True)

    status: str
    service: str
    version: str
    git_sha: str
    checks: dict[str, _CheckDetail]
    reasons: list[str]


class ErrorResponse(ContractModel):
    error_id: str
    detail: str
    request_id: str | None = None


async def _rid_middleware(inner: Any, scope: Any, receive: Any, send: Any) -> None:
    if scope["type"] != "http":
        await inner(scope, receive, send)
        return
    raw = next((v for k, v in scope.get("headers") or () if k.lower() == b"x-request-id"), b"")
    with bind_request_id(raw.decode("latin1").strip() or None) as rid:
        scope.setdefault("state", {})["request_id"] = rid
        hdr = (b"x-request-id", rid.encode("latin1"))

        async def _send(msg: Any) -> None:
            if msg["type"] == "http.response.start":
                headers = [p for p in msg.get("headers", ()) if p[0].lower() != b"x-request-id"]
                msg = {**msg, "headers": [*headers, hdr]}
            await send(msg)

        await inner(scope, receive, _send)


def _attach_telemetry(app: Any, settings: OtelSettings, *, strict: bool) -> None:
    try:
        providers = _telemetry.configure_telemetry(settings, set_global=True)
        app.state.telemetry_providers = providers
        original = app.router.lifespan_context

        @asynccontextmanager
        async def _lifespan(scope_app: Any):
            try:
                async with original(scope_app) as state:
                    yield state
            finally:
                providers.shutdown()

        app.router.lifespan_context = _lifespan
        _LOGGER.info(
            "OpenTelemetry started for '%s' -> %s", settings.service_name, settings.endpoint
        )
    except Exception as exc:  # noqa: BLE001
        if strict:
            raise
        _LOGGER.warning("Could not initialize OpenTelemetry: %s", exc)


def init_service(
    app: Any,
    name: str,
    *,
    checks: Sequence[DependencyCheck] = (),
    manifest: DependencyManifest | None = None,
    enable_otel: bool = True,
    strict_otel: bool = True,
    require_issued_identity: bool = False,
    suppress_probe_telemetry: bool = True,
    log_level: int | None = logging.INFO,
    check_timeout_seconds: float = 10.0,
    health_extra: Callable[[], Mapping[str, Any]] | None = None,
) -> None:
    """Mount /livez, /readyz, /health, request-ID middleware, JSON logging, and OTel lifespan."""
    JSONResponse = require_optional("fastapi.responses", extra="fastapi").JSONResponse
    if hasattr(app.state, _INITIALIZED_ATTR):
        raise RuntimeError("init_service was already called for this app")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must not be blank")
    if check_timeout_seconds <= 0:
        raise ValueError("check_timeout_seconds must be positive")

    relax_hint = "pass strict_otel=False to log the problem and start"
    parsed_settings: OtelSettings | None = None
    active_settings: OtelSettings | None = None
    tier = EnvironmentTier.LOCAL_DEV.value

    if enable_otel:
        try:
            parsed_settings = OtelSettings.from_env(
                strict=strict_otel,
                default_service_name=name,
                require_issued_identity=require_issued_identity,
                suppress_probe_telemetry=suppress_probe_telemetry,
            )
            tier = parsed_settings.environment
            if parsed_settings.endpoint and not parsed_settings.sdk_disabled:
                active_settings = parsed_settings
            else:
                deployed = EnvironmentTier(tier) in PLATFORM_OWNED_TIERS
                _LOGGER.log(
                    logging.WARNING if deployed else logging.DEBUG,
                    "%s is not set or disabled; OpenTelemetry export is off",
                    RuntimeEnvKey.OTEL_EXPORTER_OTLP_ENDPOINT.value,
                )
        except Exception as exc:
            if strict_otel:
                if isinstance(exc, ValueError):
                    raise type(exc)(f"{exc}; {relax_hint}") from exc
                raise
            _LOGGER.warning("Could not initialize OpenTelemetry: %s", exc)
    else:
        tier = resolve_environment().tier.value

    sha = read_runtime_env(RuntimeEnvKey.GIT_COMMIT_SHA) or "unknown"
    ver = (
        parsed_settings.service_version
        if parsed_settings is not None
        else (read_runtime_env(RuntimeEnvKey.SERVICE_VERSION) or "unknown")
    )
    release_ver = ver if ver and ver != "unknown" else sha
    release_info = {"version": release_ver, "git_sha": sha}

    configure_logging(
        level=log_level,
        service_name=parsed_settings.service_name if parsed_settings else name,
        environment=parsed_settings.deployment_environment if parsed_settings else tier,
        service_version=release_ver,
        suppress_probes=suppress_probe_telemetry,
    )
    app.state.service_release = release_info
    setattr(app.state, _INITIALIZED_ATTR, name)

    orig_stack = app.build_middleware_stack
    app.build_middleware_stack = lambda: partial(_rid_middleware, orig_stack())
    app.middleware_stack = None

    all_checks = tuple(checks)

    async def report() -> tuple[int, dict[str, Any]]:
        if not all_checks and manifest is None:
            return _HTTP_OK, {"status": HealthStatus.HEALTHY.value, "checks": {}, "reasons": []}
        kw: dict[str, Any] = {"timeout_seconds": check_timeout_seconds}
        if manifest is not None:
            kw.update(manifest=manifest, tier=tier)
        return await evaluate_health(all_checks, **kw)

    tags = ["Observability"]

    @app.get("/livez", tags=tags, response_model=ProbeResponse)
    def livez():
        return JSONResponse({"status": "alive", "service": name})

    @app.get(
        "/readyz",
        tags=tags,
        response_model=ProbeResponse,
        responses={503: {"model": ProbeResponse}},
    )
    async def readyz():
        code, body = await report()
        payload: dict[str, Any] = {
            "status": "ready" if code == _HTTP_OK else "not_ready",
            "service": name,
        }
        if code != _HTTP_OK:
            payload["reasons"] = body["reasons"]
        return JSONResponse(payload, status_code=code)

    @app.get(
        "/health",
        tags=tags,
        response_model=HealthResponse,
        responses={503: {"model": HealthResponse}},
    )
    async def health():
        code, body = await report()
        extra = dict(health_extra()) if health_extra else {}
        return JSONResponse({**extra, **body, "service": name, **release_info}, status_code=code)

    if active_settings is not None:
        _attach_telemetry(app, active_settings, strict=strict_otel)


class HttpClientSettings(ContractModel):
    timeout_seconds: float = Field(default=10.0, gt=0)
    connect_timeout_seconds: float = Field(default=5.0, gt=0)
    max_connections: int = Field(default=100, ge=1)
    max_keepalive_connections: int = Field(default=20, ge=0)
    user_agent: str = ""
    follow_redirects: bool = False

    @model_validator(mode="after")
    def _validate_limits(self) -> HttpClientSettings:
        if self.max_keepalive_connections > self.max_connections:
            raise ValueError("max_keepalive_connections cannot exceed max_connections")
        return self


def _build_httpx_client(
    settings: HttpClientSettings | None,
    *,
    async_client: bool,
) -> Any:
    cfg = settings or HttpClientSettings()
    httpx = require_optional("httpx", extra="http")
    limits = httpx.Limits(
        max_connections=cfg.max_connections,
        max_keepalive_connections=cfg.max_keepalive_connections,
    )
    timeout = httpx.Timeout(cfg.timeout_seconds, connect=cfg.connect_timeout_seconds)
    transport_cls = httpx.AsyncHTTPTransport if async_client else httpx.HTTPTransport
    client_cls = httpx.AsyncClient if async_client else httpx.Client
    ua = cfg.user_agent or f"infra2-sdk/{__version__}"
    return client_cls(
        timeout=timeout,
        transport=transport_cls(limits=limits),
        headers={"User-Agent": ua},
        follow_redirects=cfg.follow_redirects,
    )


def create_http_client(settings: HttpClientSettings | None = None) -> Any:
    """Create a configured synchronous ``httpx.Client``."""
    return _build_httpx_client(settings, async_client=False)


def create_async_http_client(settings: HttpClientSettings | None = None) -> Any:
    """Create a configured asynchronous ``httpx.AsyncClient``."""
    return _build_httpx_client(settings, async_client=True)


def _retry_delay(
    headers: Any,
    attempt: int,
    *,
    backoff_base: float,
    max_backoff: float,
) -> float:
    raw = (
        str(headers.get("retry-after") or headers.get("Retry-After") or "").strip()
        if headers is not None
        else ""
    )
    if raw:
        try:
            return min(max_backoff, max(0.0, float(raw)))
        except ValueError:
            try:
                dt = parsedate_to_datetime(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return min(max_backoff, max(0.0, (dt - datetime.now(UTC)).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                pass
    return min(max_backoff, backoff_base * (2**attempt))


def request_with_retry(
    client: Any,
    method: str,
    url: str,
    *,
    max_attempts: int = 3,
    retry_statuses: Collection[int] = _DEFAULT_RETRY_STATUSES,
    backoff_base: float = 0.5,
    max_backoff: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> Any:
    """Execute a synchronous httpx request honoring Retry-After and exponential backoff."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    httpx = require_optional("httpx", extra="http")
    for attempt in range(max_attempts):
        try:
            resp = client.request(method, url, **kwargs)
        except (httpx.TransportError, OSError):
            if attempt + 1 >= max_attempts:
                raise
            sleep(_retry_delay(None, attempt, backoff_base=backoff_base, max_backoff=max_backoff))
            continue
        if int(resp.status_code) not in retry_statuses or attempt + 1 >= max_attempts:
            return resp
        sleep(
            _retry_delay(resp.headers, attempt, backoff_base=backoff_base, max_backoff=max_backoff)
        )
    raise AssertionError("unreachable")  # pragma: no cover


async def probe_http(
    url: str,
    *,
    name: str = "http",
    client: Any | None = None,
    settings: HttpClientSettings | None = None,
) -> ProbeResult:
    """Treat HTTP < 500 as reachable while recording status evidence."""
    started = time.perf_counter()
    owned = client is None
    transport = client
    try:
        transport = transport or create_async_http_client(settings)
        resp = await transport.get(url)
        code = int(resp.status_code)
        status = DependencyStatus.PRESENT if code < 500 else DependencyStatus.ABSENT
        return ProbeResult(
            name=name,
            status=status,
            detail=f"HTTP {code}",
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            name=name,
            status=DependencyStatus.ABSENT,
            detail=f"{type(exc).__name__}: {exc}",
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    finally:
        if owned and transport is not None:
            await transport.aclose()


class RateLimitWindow:
    """Thread-safe sliding-window request or token budget tracker."""

    def __init__(
        self,
        max_requests: int,
        window_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_requests <= 0 or window_seconds <= 0:
            raise ValueError("max_requests and window_seconds must be positive")
        self.limit = max_requests
        self.window_seconds = float(window_seconds)
        self._clock = clock
        self._events: deque[tuple[float, int]] = deque()
        self._used = 0
        self._lock = threading.Lock()

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._events and self._events[0][0] <= cutoff:
            _, cost = self._events.popleft()
            self._used -= cost

    def acquire(self, cost: int = 1) -> tuple[bool, float]:
        """Consume ``cost`` units when within budget; return ``(allowed, retry_after_seconds)``."""
        if cost <= 0 or cost > self.limit:
            raise ValueError("cost must be between 1 and limit")
        with self._lock:
            now = self._clock()
            self._evict(now)
            if self._used + cost <= self.limit:
                self._events.append((now, cost))
                self._used += cost
                return True, 0.0
            freed = 0
            wait_until = now + self.window_seconds
            for ts, entry_cost in self._events:
                freed += entry_cost
                if self._used - freed + cost <= self.limit:
                    wait_until = ts + self.window_seconds
                    break
            return False, max(0.0, wait_until - now)

    def used(self) -> int:
        with self._lock:
            self._evict(self._clock())
            return self._used


__all__ = (
    "DependencyKind",
    "DependencyStatus",
    "HealthStatus",
    "Dependency",
    "DependencyManifest",
    "ProbeResult",
    "DependencyCheck",
    "CheckOutcome",
    "CallableCheck",
    "run_probes",
    "evaluate_health",
    "ProbeResponse",
    "HealthResponse",
    "ErrorResponse",
    "init_service",
    "HttpClientSettings",
    "create_http_client",
    "create_async_http_client",
    "request_with_retry",
    "probe_http",
    "RateLimitWindow",
)
