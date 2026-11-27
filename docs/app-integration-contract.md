# App Integration Contract (`v4.14.2`)

This contract defines how an application integrates with `infra2-sdk` `v4.14.2` ([README.md](README.md) specifies extras and exports; `src/infra2_sdk/data/shared_env.yaml` and `stages.yaml` are the runtime data contracts).

1. **Pin one SDK release**:
   - Lock the exact wheel URL and SHA-256 from GitHub Releases (`wangzitian0/infra2-sdk`) and install only the extras the service calls (`[fastapi]`, `[postgres]`, `[s3]`, `[llm]`, `[telemetry]`, `[http]`).

2. **Declare and validate configuration (`infra2_sdk.config`)**:
   - Declare canonical variable names from `RuntimeEnvKey` and `src/infra2_sdk/data/shared_env.yaml`.
   - Validate manifests offline with `config.EnvironmentManifest.from_model(Settings).validate_offline()`.
   - Enforce startup config via `config.assert_env(manifest, require_injected=...)` or `python -m infra2_sdk.config validate <manifest>`. Note: CLI `validate` defaults to `require_injected=True` (`--no-require-injected` disables it), while Python `assert_env()` defaults to `require_injected=False`.

3. **Bootstrap HTTP service, outbound clients, and telemetry (`infra2_sdk.service`, `infra2_sdk.telemetry`)**:
   - HTTP apps: call `service.init_service(app, name, checks=..., manifest=...)` once to register `/livez`, `/readyz`, `/health`, `X-Request-ID` propagation, probe exclusion, and OTel lifespan; start with `service.serve(app, ...)` and stream text with `service.sse_stream_response(...)`.
   - Workers: wrap execution in `telemetry.worker_run(name, ...)` (or `telemetry.init_worker(name, ...)`), enrich logs with `telemetry.bind_log_context(...)`, and instrument spans/metrics via `telemetry.get_tracer()` / `telemetry.get_meter()`.
   - Outbound HTTP: create clients with `service.create_http_client()` / `create_async_http_client()` and call `service.request_with_retry()` / `async_request_with_retry()` to propagate `X-Request-ID` and `traceparent`.

4. **Own storage adapters and pre-deploy schema gate (`infra2_sdk.storage`)**:
   - Read DB/Redis/S3 config via `PostgresSettings.from_env()`, `RedisSettings.from_env()`, and `S3Settings.from_env()`.
   - Use `storage.presign_get_url(settings, key, public=True)` with `S3_PUBLIC_ENDPOINT` for browser-facing object links, and `storage.redact_presigned_url()` / `redact_postgres_error()` / `redact_redis_error()` before logging or returning errors.
   - Install `infra2-sdk[postgres]` (plus consumer `alembic` if checking heads) and run `python -m infra2_sdk.storage check-schema` (`--metadata`, `--import`, `--alembic-config`, `--alembic-script-location`, `--precondition`, `--timeout`, `--json`). Enum validation checks label-set membership (`0` ok, `1` blocked, `3` not evaluated).

5. **Call LLMs through one guarded entrypoint (`infra2_sdk.llm`)**:
   - Build models with `llm.LlmSettings.from_env()`, versioned `llm.DecodingSpec`, and `llm.create_llm_agent(..., call_site=...)` (auto-wires OTel `gen_ai.*` telemetry, error scrubbing, and `is_retryable_llm_error`).
   - Stream deltas safely via `llm.stream_text()`, bind run digests with `llm.llm_call_context()` + `llm.schema_sha256()`, and scrub uncaught upstream faults with `llm.redact_llm_error()`.

6. **Declare and run delivery hooks (`infra2_sdk.delivery`)**:
   - Declare Schema v3 `runtime-hooks.manifest.json` (`delivery.RuntimeHooksManifest`) against `delivery.CELL_POLICIES` (`stages.yaml`). Checks with `writes=True` are forbidden in `prod`.
   - Validate and execute hooks with `python -m infra2_sdk.delivery check-hooks|run-cell|poll`.
