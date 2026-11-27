# App integration contract

This page states how an application integrates with `infra2-sdk` `v4.14.2` (`README.md` defines packages and exports; `src/infra2_sdk/data/shared_env.yaml` and `stages.yaml` are the runtime data contracts).

1. **Pin one SDK release**: lock the release wheel URL + SHA-256 from GitHub Releases (`wangzitian0/infra2-sdk`).
2. **Declare & validate config (`infra2_sdk.config`)**:
   - Read only canonical variable names in `RuntimeEnvKey` / `src/infra2_sdk/data/shared_env.yaml`; validate manifests with `config.EnvironmentManifest.from_model(Settings).validate_offline()`.
   - Enforce startup config via `config.assert_env(...)` or `python -m infra2_sdk.config export|check|validate`.
3. **Bootstrap HTTP, streaming & telemetry (`infra2_sdk.service`, `infra2_sdk.telemetry`, `infra2_sdk.llm`)**:
   - Call `service.init_service(app, name, ...)` once for HTTP apps (optionally run with `service.serve()` and emit SSE streams via `service.sse_stream_response()`) or `telemetry.worker_run(...)` / `telemetry.init_worker(name, ...)` for workers to configure JSON stdout logs, request correlation, and OTLP HTTP export.
   - Use `service.create_http_client()` / `create_async_http_client()` and `request_with_retry()` / `async_request_with_retry()` for outbound calls with `X-Request-ID` + `traceparent` propagation.
   - Use `llm.LlmSettings`, `llm.DecodingSpec`, `llm.create_llm_agent()`, `llm.stream_text()`, `llm.is_retryable_llm_error()`, and `llm.redact_llm_error()` for instrumented LLM calls.
4. **Own your schema, S3 presign & delivery hooks (`infra2_sdk.storage`, `infra2_sdk.delivery`)**:
   - Use `storage.presign_get_url(..., public=True)` with `S3_PUBLIC_ENDPOINT` when issuing browser-accessible object links, and `infra2-sdk[postgres]` (plus consumer `alembic` if checking migration heads) for `python -m infra2_sdk.storage check-schema --metadata ...`.
   - Declare a Schema v3 `runtime-hooks.manifest.json` (`delivery.RuntimeHooksManifest`) and validate/run with `python -m infra2_sdk.delivery check-hooks|run-cell|poll`.
