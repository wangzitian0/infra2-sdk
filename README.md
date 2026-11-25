# infra2-sdk (`v4.10.0`)

Standalone cross-repository runtime contracts and explicit open-protocol adapters for `infra2`, consumer applications, and `canary/todo`.

## 1. Architecture & Core Dependencies

- **Six flat public modules (`infra2_sdk.*`)**, lazy-loaded with zero I/O or global provider registration on import: `config`, `telemetry`, `service`, `storage`, `llm`, `delivery`.
- **Two core dependencies only** (`pydantic>=2.7,<3`, `pyyaml>=6.0,<7`). Every wire/manifest/settings contract inherits `ContractModel(pydantic.BaseModel)` (`frozen=True`, `extra="forbid"`, `populate_by_name=True`). No `pydantic-settings` dependency is required.
- **No legacy compatibility shims in v4.10.0**: consumers pin exact released versions and migrate forward cleanly; retired variable names (`ENV`, `APP_ENV`, `IMAGE_TAG`, `S3_ENDPOINT_URL`, etc.) and control-plane-only helpers are removed.

## 2. Installation Extras

| Install Spec | Packages Added | Modules Unlocked |
|---|---|---|
| `infra2-sdk==4.10.0` | `pydantic`, `pyyaml` | All 6 modules (core contracts, RESP Redis probe, stdlib health poll, hooks CLI) |
| `infra2-sdk[telemetry]==4.10.0` | `opentelemetry-api/sdk/exporter-otlp-proto-http/instrumentation-logging` | `telemetry.configure_telemetry`, `telemetry.init_worker` |
| `infra2-sdk[http]==4.10.0` | `httpx` | `service.create_http_client`, `service.create_async_http_client`, `service.request_with_retry`, `service.probe_http` |
| `infra2-sdk[fastapi]==4.10.0` | `fastapi`, `telemetry` extra, `http` extra | `service.init_service` (`/livez`, `/readyz`, `/health`, OTel lifespan, `X-Request-ID`) |
| `infra2-sdk[postgres]==4.10.0` | `psycopg` | `storage.probe_postgres`, `storage.check_schema`, `python -m infra2_sdk.storage check-schema` |
| `infra2-sdk[s3]==4.10.0` | `boto3` | `storage.create_s3_client`, `storage.ensure_bucket`, `storage.probe_s3`, `storage.put_object_bytes`, `storage.read_object_bytes` |
| `infra2-sdk[llm]==4.10.0` | `pydantic-ai-slim[openai]`, `openai`, `http` extra, `telemetry` extra | `llm.create_llm_model`, `llm.create_llm_agent`, `llm.GuardedLlmModel` |
| `infra2-sdk[all]==4.10.0` | All optional extras | Full SDK runtime surface |

## 3. Public Module Surface (MECE 6-Module Contract)

| Module | Ownership | Key Exports & Entrypoints |
|---|---|---|
| `infra2_sdk.config` | Canonical environment tiers, `RuntimeEnvKey`, `shared_env.yaml` validation, Pydantic `EnvironmentManifest` & startup guard | `EnvironmentTier`, `RuntimeEnvKey`, `EnvironmentManifest`, `resolve_tier()`, `resolve_environment()`, `read_runtime_env()`, `shared_env_contract()`, `check_shared_env()`, `validate_env()`, `assert_env()`, CLI `python -m infra2_sdk.config {export\|check\|validate}` |
| `infra2_sdk.telemetry` | OTLP HTTP resource identity (`OtelSettings`), JSON stdout logging (`JsonLogFormatter`), request/log `ContextVar`s, probe noise suppression, worker bootstrap | `OtelSettings`, `TelemetryProviders`, `JsonLogFormatter`, `PROBE_PATHS`, `REQUIRED_IDENTITY_ATTRIBUTES`, `current_request_id()`, `bind_request_id()`, `bind_log_context()`, `configure_logging()`, `signal_endpoint()`, `configure_telemetry()`, `init_worker()` |
| `infra2_sdk.service` | Dependency manifest & async probe runner, FastAPI `/livez`/`/readyz`/`/health` bootstrap, `httpx` retry/probe helpers, sliding-window rate limiter | `Dependency`, `DependencyManifest`, `ProbeResult`, `CallableCheck`, `run_probes()`, `evaluate_health()`, `init_service()`, `HttpClientSettings`, `create_http_client()`, `create_async_http_client()`, `request_with_retry()`, `probe_http()`, `RateLimitWindow` |
| `infra2_sdk.storage` | PostgreSQL DSN & `SELECT 1` probe, stdlib RESP Redis probe, S3 helpers & URL redaction, PostgreSQL schema/enum rollback gate (`ROLLBACK_CLASS_A`/`C`) | `PostgresSettings`, `normalize_postgres_dsn()`, `probe_postgres()`, `redact_postgres_error()`, `RedisSettings`, `probe_redis()`, `redact_redis_error()`, `EnumSchemaSpec`, `EnumDiscrepancy`, `SchemaDiffReport`, `enums_from_metadata()`, `check_enums()`, `classify_rollback()`, `check_schema()`, `S3Settings`, `create_s3_client()`, `ensure_bucket()`, `probe_s3()`, `put_object_bytes()`, `read_object_bytes()`, `redact_presigned_url()`, CLI `python -m infra2_sdk.storage check-schema` |
| `infra2_sdk.llm` | `LlmSettings` (role/provider config), `DecodingSpec`, prompt/schema/request SHA-256 fingerprints, 4-stream OTel `GuardedLlmModel`, `Agent` factory, error scrubbing, VCR config | `LlmSettings`, `DecodingSpec`, `ModelRole`, `ThinkingLevel`, `GuardedLlmModel`, `create_llm_model()`, `create_llm_agent()`, `llm_call_context()`, `prompt_sha256()`, `schema_sha256()`, `request_sha256()`, `redact_llm_error()`, `vcr_llm_config()`, `allow_vcr_model_requests()` |
| `infra2_sdk.delivery` | Packaged `stages.yaml` cell policy (`CELL_POLICIES`), schema v3 `RuntimeHooksManifest` runner/allowlist, deploy provenance contracts & HTTP convergence poller | `CELL_POLICIES`, `load_stages()`, `RuntimeHooksManifest`, `CheckRow`, `OverrideReason`, `resolve_check_cells()`, `build_hook_env()`, `run_cell()`, `record_verdicts()`, `DeployRequest`, `DeployEvidence`, `poll_until_healthy()`, CLI `python -m infra2_sdk.delivery {check-hooks\|run-cell\|poll}` |

## 4. Migration (`v4.9.1 -> v4.10.0`) & Canary Proof

- `infra2_sdk.runtime.environment`, `runtime.config_schema` -> `infra2_sdk.config` (`assert_environment` -> `assert_env`, `check_shared_env_conformance` -> `check_shared_env`)
- `infra2_sdk.runtime.otel` -> `infra2_sdk.telemetry` (`OtelSettings.resolved_resource_attributes()`)
- `infra2_sdk.fastapi`, `runtime.health`, `runtime.http` -> `infra2_sdk.service` (`check_health` -> `evaluate_health`)
- `infra2_sdk.runtime.{postgres,redis,s3,schema_check}` -> `infra2_sdk.storage` (`load_code_enums` -> `enums_from_metadata`)
- `infra2_sdk.runtime.llm` -> `infra2_sdk.llm` (`LlmSettings.from_mapping` -> `LlmSettings.from_env`)
- `infra2_sdk.manifests`, `deploy`, `ci` -> `infra2_sdk.delivery` (`load_hooks_manifest` -> `RuntimeHooksManifest.from_file`)
- **Removed from SDK** (control-plane only, stay in `infra2` or pinned `4.9.x`): `rules/*`, `secrets`, `routing`, `observability`, `capacity`, `refs`, `stage_record`, `transport`.
- **Canary Todo proof**: `pytest canary/todo/tests -q` and `scripts/check_canary_standalone.sh` verify all six modules end-to-end against real PostgreSQL, Redis, RustFS S3, OTLP HTTP stub, worker probes, and VCR LLM replay.
