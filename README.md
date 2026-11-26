# infra2-sdk (`v4.12.0`)

Standalone cross-repository runtime contracts and explicit open-protocol adapters for `infra2`, consumer applications, and `canary/todo`.

## 1. Architecture & Core Dependencies

- **Six flat public modules (`infra2_sdk.*`)**, lazy-loaded with zero I/O or global provider registration on import: `config`, `telemetry`, `service`, `storage`, `llm`, `delivery`.
- **Single internal base (`infra2_sdk._core`)**: `ContractModel(pydantic.BaseModel)` (`frozen=True`, `extra="forbid"`, `populate_by_name=True`), shared URL/log redaction, telemetry bootstrap plumbing, and `require_optional`. Core depends only on `pydantic>=2.7,<3` and `pyyaml>=6.0,<7`.
- **Single-interface releases**: consumers pin exact released wheels and migrate forward; superseded Generation-1 wrappers and control-plane helpers are deleted from the SDK surface.

## 2. Installation Extras

| Install Spec | Packages Added | Capabilities Unlocked |
|---|---|---|
| `infra2-sdk==4.12.0` | `pydantic`, `pyyaml` | All 6 modules (contracts, `probe_redis`, hooks/schema/config CLIs) |
| `infra2-sdk[telemetry]==4.12.0` | `opentelemetry-api/sdk/exporter-otlp-proto-http/instrumentation-logging` | `telemetry.init_worker`, `telemetry.get_tracer`, `telemetry.get_meter`, `TelemetryProviders` |
| `infra2-sdk[http]==4.12.0` | `httpx` | `service.create_http_client`, `service.create_async_http_client`, `service.request_with_retry`, `service.async_request_with_retry`, `service.probe_http` |
| `infra2-sdk[fastapi]==4.12.0` | `fastapi`, `telemetry` extra, `http` extra | `service.init_service` (`/livez`, `/readyz`, `/health`, OTel lifespan, probe exclusion, `X-Request-ID`) |
| `infra2-sdk[postgres]==4.12.0` | `psycopg[binary]` | `storage.probe_postgres`, `python -m infra2_sdk.storage check-schema` |
| `infra2-sdk[s3]==4.12.0` | `boto3` | `storage.create_s3_client`, `storage.ensure_bucket`, `storage.probe_s3`, `storage.put_object_bytes`, `storage.read_object_bytes` |
| `infra2-sdk[llm]==4.12.0` | `pydantic-ai-slim[zai]`, `http` extra, `telemetry` extra | `llm.create_llm_agent`, `llm.llm_call_context`, `llm.redact_llm_error`, `llm.DecodingSpec` |
| `infra2-sdk[all]==4.12.0` | All optional extras | Full SDK runtime surface |

## 3. Public Module Surface (MECE 6-Module Contract — 54 Exports)

| Module | Ownership | Key Exports & Entrypoints |
|---|---|---|
| `infra2_sdk.config` | Canonical environment tiers, `RuntimeEnvKey`, Pydantic `EnvironmentManifest` & startup guard | `EnvironmentTier`, `RuntimeEnvKey`, `EnvironmentField`, `EnvironmentManifest`, `resolve_tier()`, `read_runtime_env()`, `assert_env()`, CLI `python -m infra2_sdk.config {export\|check\|validate}` |
| `infra2_sdk.telemetry` | `TelemetryProviders`, tracer/meter helpers, request/log `ContextVar`s, and worker OTLP bootstrap | `TelemetryProviders`, `get_tracer()`, `get_meter()`, `current_request_id()`, `bind_log_context()`, `init_worker()` |
| `infra2_sdk.service` | Dependency manifest & checks, FastAPI `/livez`/`/readyz`/`/health` bootstrap, `httpx` retry/probe helpers, rate limiter | `DependencyKind`, `DependencyStatus`, `Dependency`, `DependencyManifest`, `ProbeResult`, `CallableCheck`, `init_service()`, `HttpClientSettings`, `create_http_client()`, `create_async_http_client()`, `request_with_retry()`, `async_request_with_retry()`, `probe_http()`, `RateLimitWindow` |
| `infra2_sdk.storage` | PostgreSQL/Redis/S3 settings & probes, S3 helpers & URL/error redaction, schema-gate CLI | `PostgresSettings`, `probe_postgres()`, `redact_postgres_error()`, `RedisSettings`, `probe_redis()`, `redact_redis_error()`, `S3Settings`, `create_s3_client()`, `ensure_bucket()`, `is_not_found()`, `put_object_bytes()`, `read_object_bytes()`, `redact_presigned_url()`, `probe_s3()`, CLI `python -m infra2_sdk.storage check-schema` |
| `infra2_sdk.llm` | `LlmSettings`, `DecodingSpec`, SHA-256 digests, OTel `create_llm_agent()` factory, and error scrubbing | `LlmSettings`, `DecodingSpec`, `create_llm_agent()`, `llm_call_context()`, `prompt_sha256()`, `schema_sha256()`, `request_sha256()`, `redact_llm_error()` |
| `infra2_sdk.delivery` | Lazy `stages.yaml` policy (`CELL_POLICIES`), schema v3 `RuntimeHooksManifest`, cell routing & hooks/poll CLI | `CELL_POLICIES`, `CellPolicy`, `CheckRow`, `RuntimeHooksManifest`, `resolve_check_cells()`, CLI `python -m infra2_sdk.delivery {check-hooks\|run-cell\|poll}` |

## 4. Migration & Canary Proof

- Consolidated runtime adapters into 54 public exports across `infra2_sdk.{config,telemetry,service,storage,llm,delivery}` (`README.md` is the public API reference).
- Standalone Canary proof: `pytest canary/todo/tests -q` and `scripts/check_canary_standalone.sh` exercise all six modules against real PostgreSQL, Redis, RustFS S3, OTLP HTTP stub, worker probes, `check-schema`, `check-hooks`/`run-cell`, and LLM summary replay.
