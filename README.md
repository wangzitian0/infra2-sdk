# infra2-sdk (`v4.14.2`)

Standalone runtime contracts and open-protocol adapters for `infra2`, consumer applications, and `canary/todo`.

## 1. Architecture & Guarantees

- **Six flat lazy modules (`infra2_sdk.*`)**: `config`, `telemetry`, `service`, `storage`, `llm`, and `delivery`. Importing the package or any submodule performs zero disk, network, or global provider I/O.
- **Minimal core (`infra2_sdk._core`)**: depends only on `pydantic>=2.7,<3` and `pyyaml>=6.0,<7`. Shared contracts inherit frozen `ContractModel` (`extra="forbid"`, `populate_by_name=True`).
- **Explicit open-protocol adapters**: optional libraries (`opentelemetry`, `httpx`, `fastapi`, `uvicorn`, `psycopg`, `boto3`, `pydantic-ai-slim`) load only when an adapter runs.
- **Single-interface releases**: consumers pin exact wheel URLs and SHA-256 hashes; retired shims and control-plane logic stay out of the SDK.

## 2. Installation Extras

| Install Spec | Packages Added | Capabilities Unlocked |
|---|---|---|
| `infra2-sdk==4.14.2` | `pydantic`, `pyyaml` | All 6 modules: pure contracts, `probe_redis`, `config` and `delivery` CLIs, no-op tracers/meters, stdout JSON logs when OTLP is unset |
| `infra2-sdk[telemetry]==4.14.2` | `opentelemetry-api/sdk/exporter-otlp-proto-http/instrumentation-logging` | OTLP HTTP export (traces, metrics, logs) for `init_worker`, `worker_run`, `get_tracer`, `get_meter`, `TelemetryProviders` |
| `infra2-sdk[http]==4.14.2` | `httpx` | `create_http_client`, `create_async_http_client`, `request_with_retry`, `async_request_with_retry`, `probe_http` |
| `infra2-sdk[fastapi]==4.14.2` | `fastapi`, `uvicorn[standard]`, `telemetry` extra, `http` extra | `service.init_service` (`/livez`, `/readyz`, `/health`, OTel lifespan, probe exclusion on `/livez`/`/readyz`/`/health`/`/api/health`, `X-Request-ID`), `service.sse_stream_response`, `service.serve` |
| `infra2-sdk[postgres]==4.14.2` | `psycopg[binary]` | `storage.probe_postgres`, `storage.check_schema_cli`, `python -m infra2_sdk.storage check-schema` (consumer `alembic` required only with `--alembic-config` / `--alembic-script-location`) |
| `infra2-sdk[s3]==4.14.2` | `boto3` | `storage.create_s3_client`, `storage.ensure_bucket`, `storage.probe_s3`, `storage.put_object_bytes`, `storage.read_object_bytes`, `storage.presign_get_url` |
| `infra2-sdk[llm]==4.14.2` | `pydantic-ai-slim[zai]`, `http` extra, `telemetry` extra | `llm.create_llm_agent` (`LlmSettings`, `DecodingSpec`, `stream_text`, `llm_call_context`, `is_retryable_llm_error`, `redact_llm_error`, and `schema_sha256` import from core) |
| `infra2-sdk[all]==4.14.2` | All optional extras | Full SDK runtime surface |

## 3. Public Module Surface (MECE 6-Module Contract — 58 Exports)

| Module | Ownership | Key Exports & Entrypoints |
|---|---|---|
| `infra2_sdk.config` | Canonical environment tiers, `RuntimeEnvKey`, Pydantic `EnvironmentManifest` & startup guard | `EnvironmentTier`, `RuntimeEnvKey`, `EnvironmentField`, `EnvironmentManifest`, `resolve_tier()`, `assert_env()`, CLI `python -m infra2_sdk.config {export\|check\|validate}` |
| `infra2_sdk.telemetry` | `TelemetryProviders`, tracer/meter helpers, log context binding, and worker OTLP lifecycle | `TelemetryProviders`, `get_tracer()`, `get_meter()`, `current_request_id()`, `bind_log_context()`, `init_worker()`, `worker_run()` |
| `infra2_sdk.service` | Dependency manifest & checks, FastAPI `/livez`/`/readyz`/`/health` + SSE & uvicorn server bootstrap, `httpx` retry/probe helpers, rate limiter | `DependencyKind`, `DependencyStatus`, `Dependency`, `DependencyManifest`, `ProbeResult`, `CallableCheck`, `init_service()`, `sse_stream_response()`, `serve()`, `HttpClientSettings`, `create_http_client()`, `create_async_http_client()`, `request_with_retry()`, `async_request_with_retry()`, `probe_http()`, `RateLimitWindow` |
| `infra2_sdk.storage` | PostgreSQL/Redis/S3 settings & probes, S3 helpers & presigned GET / URL/error redaction, schema-gate CLI | `PostgresSettings`, `probe_postgres()`, `redact_postgres_error()`, `RedisSettings`, `probe_redis()`, `redact_redis_error()`, `S3Settings`, `check_schema_cli()`, `create_s3_client()`, `ensure_bucket()`, `is_not_found()`, `put_object_bytes()`, `read_object_bytes()`, `presign_get_url()`, `redact_presigned_url()`, `probe_s3()`, CLI `python -m infra2_sdk.storage check-schema` |
| `infra2_sdk.llm` | `LlmSettings`, `DecodingSpec`, schema digest, OTel `create_llm_agent()`, SSE text streamer, and error scrubbing | `LlmSettings`, `DecodingSpec`, `create_llm_agent()`, `stream_text()`, `is_retryable_llm_error()`, `llm_call_context()`, `schema_sha256()`, `redact_llm_error()` |
| `infra2_sdk.delivery` | Lazy `stages.yaml` policy (`CELL_POLICIES`), schema v3 `RuntimeHooksManifest`, cell routing & hooks/poll CLI | `CELL_POLICIES`, `CellPolicy`, `CheckRow`, `RuntimeHooksManifest`, `resolve_check_cells()`, CLI `python -m infra2_sdk.delivery {check-hooks\|run-cell\|poll}` |

## 4. Quickstart

### HTTP Service (`config` + `service`)

```python
from fastapi import FastAPI
from infra2_sdk.config import EnvironmentManifest, assert_env
from infra2_sdk.service import CallableCheck, init_service, serve

manifest = EnvironmentManifest.from_model(Settings, source="app/api")
assert_env(manifest)

app = FastAPI()
init_service(app, "my-api", checks=[CallableCheck("cache", check_cache)])
serve(app, host="0.0.0.0", port=8000)
```

### Worker Lifecycle & Outbound HTTP (`telemetry` + `service`)

```python
from infra2_sdk.service import HttpClientSettings, create_http_client, request_with_retry
from infra2_sdk.telemetry import bind_log_context, worker_run

with worker_run("my-worker"), bind_log_context(job="sync"):
    with create_http_client(HttpClientSettings(timeout_seconds=5.0)) as client:
        resp = request_with_retry(client, "GET", "https://api.example.com/health")
```

### Storage & Instrumented LLM Streaming (`storage` + `llm` + `service`)

```python
from infra2_sdk.llm import DecodingSpec, LlmSettings, create_llm_agent, redact_llm_error, stream_text
from infra2_sdk.service import sse_stream_response
from infra2_sdk.storage import S3Settings, presign_get_url

s3 = S3Settings.from_env()
url = presign_get_url(s3, "reports/latest.pdf", public=True)

agent = create_llm_agent(
    LlmSettings.from_env(),
    role="small",
    decoding=DecodingSpec(decoding_version="v1", max_tokens=512),
    call_site="api.summary",
)
# Inside an async FastAPI route:
return await sse_stream_response(
    stream_text(agent, "Summarize today."),
    meta={"model": agent.model.wrapped.model_name},
    scrub_error=redact_llm_error,
)
```

### CLI Entrypoints

| Command | Purpose & Key Flags | Exit Codes |
|---|---|---|
| `python -m infra2_sdk.config export <mod:Settings> --json <path>` | Export Settings model as an `EnvironmentManifest` JSON file | `0` ok, `1` error |
| `python -m infra2_sdk.config check <mod:Settings> --json <path>` | Fail if checked-in manifest differs from `<mod:Settings>` | `0` match, `1` drift |
| `python -m infra2_sdk.config validate <manifest.json> [--no-require-injected]` | Validate process `os.environ` against manifest (`require_injected=True` default) | `0` valid, `1` missing |
| `python -m infra2_sdk.storage check-schema [--metadata <mod:ATTR>] [--import <mod>] [--alembic-config <ini>] [--alembic-script-location <dir>] [--precondition <sql>] [--timeout 15.0] [--json]` | Verify Postgres tables, set-membership enum labels, Alembic heads, and read-only SQL preconditions | `0` ok, `1` blocked, `3` not evaluated |
| `python -m infra2_sdk.delivery check-hooks <manifest.json>` | Validate Schema v3 `RuntimeHooksManifest` and cell schedule | `0` valid, `1` invalid |
| `python -m infra2_sdk.delivery run-cell <manifest.json> --cell <cell> [--cwd <dir>]` | Run manifest checks assigned to `<cell>` within stage budget | `0` pass, `1` fail |
| `python -m infra2_sdk.delivery poll --url <url> [--expected-version <v>] [--timeout-seconds 60] [--interval-seconds 2] [--follow-redirects]` | Poll health JSON endpoint until HTTP 200 and version match | `0` healthy, `1` timeout |

## 5. Data Contracts & Environment Knobs

- **Canonical variable registry**: [`src/infra2_sdk/data/shared_env.yaml`](src/infra2_sdk/data/shared_env.yaml) defines shared runtime, Postgres/Redis, S3, LLM, and frontend telemetry keys.
- **Delivery stage budgets**: [`src/infra2_sdk/data/stages.yaml`](src/infra2_sdk/data/stages.yaml) is the single source of stage cells, budgets, and capabilities (`delivery.CELL_POLICIES`).
- **Additional runtime and hook knobs**:
  - S3 & Telemetry: `S3_USE_PATH_STYLE`, `OTEL_SDK_DISABLED`, `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG`, `OTEL_METRIC_EXPORT_INTERVAL`, `GITHUB_ACTIONS`.
  - Delivery hook runner (`delivery run-cell`): `APP_URL`, `TARGET_URL` passthrough; injects `HOOK_CELL`, `HOOK_SCALE`, `HOOK_ARTIFACTS_DIR`.

## 6. Further Documentation & Canary Proof

- App onboarding and contract checklist: [docs/app-integration-contract.md](docs/app-integration-contract.md)
- Architectural charter and P1–P5 boundaries: [init.md](init.md)
- Development and release guide: [CONTRIBUTING.md](CONTRIBUTING.md)
- Canary Todo reference app: [canary/todo/README.md](canary/todo/README.md) (`pytest canary/todo/tests -q` and `scripts/check_canary_standalone.sh`)

## Compatibility

- Semantic versions describe the public Python and serialized JSON contracts.
- Additive fields and enum values require a minor release.
- Removing or changing an existing field requires a major release.
- While infra2, finance_report and truealpha are the only consumers, a minor release may remove a
  public name. It may also change the default of a keyword. The census must cover the production
  and test code of the three consumers. It must find no use of the name and no reliance on the
  old default. The CHANGELOG states the change. A change that a consumer relies on needs a major
  release.
- Receivers must require a JSON integer `contract_version` and reject booleans or unsupported
  values before side effects.
- Removed names and their replacements are listed in [CHANGELOG.md](CHANGELOG.md).
