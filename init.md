# infra2-sdk Project Charter (init.md)

## 0. Purpose and Boundary

`infra2-sdk` provides cross-repository runtime contracts and explicit open-protocol adapters for `infra2`, consumer applications, and the in-repo `canary/todo` service.

- **Runtime boundary**: Applications read ordinary environment variables directly via Pydantic models and `infra2_sdk.config`. Managed deployments inject those same canonical names (`ENVIRONMENT`, `DATABASE_URL`, `REDIS_URL`, `S3_*`, `LLM_*`, `OTEL_*`) without runtime `INFRA2_*` or Vault coupling.
- **Control-plane separation**: Deployment graph compilation, Traefik routing, Vault templating, and infrastructure capacity orchestration live in `infra2`, never inside `infra2-sdk`.

## 1. Core Design Principles

1. **Zero-I/O on import & minimal core (`pyproject.toml`)**: Core depends only on `pydantic>=2.7,<3` and `pyyaml>=6.0,<7`. Optional adapters (`telemetry`, `http`, `fastapi`, `postgres`, `s3`, `llm`) load lazily behind `infra2_sdk._core.require_optional`.
2. **One authoritative contract class**: Shared contracts inherit `infra2_sdk._core.ContractModel` (`frozen=True`, `extra="forbid"`, `populate_by_name=True`).
3. **Clean versioned releases**: Every minor release maintains a strict MECE surface with positive architecture/invariant tests and zero legacy shims or tombstone tests.
4. **End-to-end Canary proof**: `canary/todo` lives in this repository and exercises all six public modules against live PostgreSQL, Redis, RustFS S3, OTLP HTTP, worker probes, and VCR LLM cassettes.

## 2. Six-Module Architecture (`src/infra2_sdk/`)

| Module | Internal Helpers | Responsibility |
|---|---|---|
| `infra2_sdk.config` | — | Environment tiers, `RuntimeEnvKey`, `shared_env.yaml`, `EnvironmentManifest` & startup validation CLI |
| `infra2_sdk.telemetry` | — | `OtelSettings`, OTLP HTTP exporters, `JsonLogFormatter`, `ContextVar` correlation, probe log filter, worker init |
| `infra2_sdk.service` | — | `DependencyManifest`, async probes & `evaluate_health`, FastAPI bootstrap (`init_service`), `httpx` retry/probe, `RateLimitWindow` |
| `infra2_sdk.storage` | `_schema_gate.py` | PostgreSQL DSN/probe, stdlib RESP Redis probe, S3 helpers/redaction, single-connection schema & enum gate (`check_schema`) |
| `infra2_sdk.llm` | `_llm_runtime.py` | `LlmSettings`, `DecodingSpec`, SHA-256 request fingerprints, 4-stream OTel `GuardedLlmModel`, `Agent` factory, error scrubbing, VCR config |
| `infra2_sdk.delivery` | — | `stages.yaml` (`CELL_POLICIES`), schema v3 `RuntimeHooksManifest` cell runner, deploy contracts, `poll_until_healthy` |
