# infra2-sdk (`v4.11.0`)

Standalone cross-repository runtime contracts and explicit open-protocol adapters for `infra2`, consumer applications, and `canary/todo`.

## 1. Architecture & Core Dependencies

- **Six flat public modules (`infra2_sdk.*`)**, lazy-loaded with zero I/O or global provider registration on import: `config`, `telemetry`, `service`, `storage`, `llm`, `delivery`.
- **Single internal base (`infra2_sdk._core`)**: `ContractModel(pydantic.BaseModel)` (`frozen=True`, `extra="forbid"`, `populate_by_name=True`), shared URL/log redaction, and `require_optional`. Core depends only on `pydantic>=2.7,<3` and `pyyaml>=6.0,<7`.
- **Clean versioned releases**: consumers pin exact released wheels and migrate forward; retired aliases and control-plane-only helpers stay out of the SDK.

## 2. Installation Extras

| Install Spec | Packages Added | Modules Unlocked |
|---|---|---|
| `infra2-sdk==4.11.0` | `pydantic`, `pyyaml` | All 6 modules (core contracts, RESP Redis probe, stdlib health poll, hooks CLI) |
| `infra2-sdk[telemetry]==4.11.0` | `opentelemetry-api/sdk/exporter-otlp-proto-http/instrumentation-logging` | `telemetry.configure_telemetry`, `telemetry.init_worker`, `telemetry.get_tracer`, `telemetry.get_meter` |
| `infra2-sdk[http]==4.11.0` | `httpx` | `service.create_http_client`, `service.create_async_http_client`, `service.request_with_retry`, `service.async_request_with_retry`, `service.probe_http` |
| `infra2-sdk[fastapi]==4.11.0` | `fastapi`, `telemetry` extra, `http` extra | `service.init_service` (`/livez`, `/readyz`, `/health`, OTel lifespan, probe exclusion, `X-Request-ID`) |
| `infra2-sdk[postgres]==4.11.0` | `psycopg[binary]` | `storage.probe_postgres`, `storage.check_schema`, `python -m infra2_sdk.storage check-schema` |
| `infra2-sdk[s3]==4.11.0` | `boto3` | `storage.create_s3_client`, `storage.ensure_bucket`, `storage.probe_s3`, `storage.put_object_bytes`, `storage.read_object_bytes` |
| `infra2-sdk[llm]==4.11.0` | `pydantic-ai-slim[zai]`, `http` extra, `telemetry` extra | `llm.create_llm_model`, `llm.create_llm_agent`, `llm.GuardedLlmModel` |
| `infra2-sdk[all]==4.11.0` | All optional extras | Full SDK runtime surface |

## 3. Public Module Surface (MECE 6-Module Contract)

| Module | Ownership | Key Exports & Entrypoints |
|---|---|---|
| `infra2_sdk.config` | Canonical environment tiers, `RuntimeEnvKey`, `shared_env.yaml` validation, Pydantic `EnvironmentManifest` & startup guard | `EnvironmentTier`, `RuntimeEnvKey`, `EnvironmentManifest`, `resolve_tier()`, `resolve_environment()`, `read_runtime_env()`, `shared_env_contract()`, `check_shared_env()`, `validate_env()`, `assert_env()`, CLI `python -m infra2_sdk.config {export\|check\|validate}` |
| `infra2_sdk.telemetry` | OTLP HTTP resource identity (`OtelSettings`), JSON stdout logging (`JsonLogFormatter`), request/log `ContextVar`s, tracer/meter helpers, worker bootstrap | `OtelSettings`, `TelemetryProviders`, `JsonLogFormatter`, `get_tracer()`, `get_meter()`, `current_request_id()`, `bind_request_id()`, `bind_log_context()`, `configure_logging()`, `signal_endpoint()`, `configure_telemetry()`, `init_worker()` |
| `infra2_sdk.service` | Dependency manifest & async probe runner, FastAPI `/livez`/`/readyz`/`/health` bootstrap, `httpx` retry/probe helpers with W3C trace propagation, sliding-window rate limiter | `Dependency`, `DependencyManifest`, `ProbeResult`, `CallableCheck`, `run_probes()`, `evaluate_health()`, `init_service()`, `HttpClientSettings`, `create_http_client()`, `create_async_http_client()`, `request_with_retry()`, `async_request_with_retry()`, `probe_http()`, `RateLimitWindow` |
| `infra2_sdk.storage` | PostgreSQL DSN & read/write probes, stdlib RESP Redis probe, S3 helpers & URL redaction, PostgreSQL schema/enum gate (`ROLLBACK_CLASS_A`/`C`) | `PostgresSettings`, `normalize_postgres_dsn()`, `probe_postgres()`, `redact_postgres_error()`, `RedisSettings`, `probe_redis()`, `redact_redis_error()`, `EnumSchemaSpec`, `EnumDiscrepancy`, `SchemaDiffReport`, `enums_from_metadata()`, `check_enums()`, `classify_rollback()`, `check_schema()`, `S3Settings`, `create_s3_client()`, `ensure_bucket()`, `probe_s3()`, `put_object_bytes()`, `read_object_bytes()`, `redact_presigned_url()`, CLI `python -m infra2_sdk.storage check-schema` |
| `infra2_sdk.llm` | `LlmSettings`, `DecodingSpec`, SHA-256 fingerprints, 4-stream OTel `GuardedLlmModel`, `Agent` factory, error scrubbing, VCR config | `LlmSettings`, `DecodingSpec`, `ModelRole`, `ThinkingLevel`, `GuardedLlmModel`, `create_llm_model()`, `create_llm_agent()`, `llm_call_context()`, `prompt_sha256()`, `schema_sha256()`, `request_sha256()`, `redact_llm_error()`, `vcr_llm_config()`, `allow_vcr_model_requests()` |
| `infra2_sdk.delivery` | Lazy `stages.yaml` policy (`CELL_POLICIES`), schema v3 `RuntimeHooksManifest` (`cells.add/remove`, `off`, `bootstrap`, `artifacts_dir`), deploy request contract & HTTP convergence poller | `CELL_POLICIES`, `load_stages()`, `RuntimeHooksManifest`, `CheckRow`, `CellOverrides`, `OverrideReason`, `resolve_check_cells()`, `build_hook_env()`, `run_cell()`, `record_verdicts()`, `DeployRequest`, `poll_until_healthy()`, CLI `python -m infra2_sdk.delivery {check-hooks\|run-cell\|poll}` |

## 4. Migration & Canary Proof

- Consolidated runtime adapters into `infra2_sdk.{config,telemetry,service,storage,llm,delivery}` (`README.md` is the public API reference).
- Standalone Canary proof: `pytest canary/todo/tests -q` and `scripts/check_canary_standalone.sh` exercise all six modules against real PostgreSQL, Redis, RustFS S3, OTLP HTTP stub, worker probes, `check-schema`, `check-hooks`/`run-cell`, and VCR LLM replay.
