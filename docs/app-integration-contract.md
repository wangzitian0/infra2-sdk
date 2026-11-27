# App integration contract

This page states how an application integrates with `infra2-sdk` `v4.13.1` (`README.md` defines packages and exports; `src/infra2_sdk/data/shared_env.yaml` and `stages.yaml` are the runtime data contracts).

1. **Pin one SDK release**: lock the release wheel URL + SHA-256 from GitHub Releases (`wangzitian0/infra2-sdk`).
2. **Declare & validate config (`infra2_sdk.config`)**:
   - Read only canonical variable names in `RuntimeEnvKey` / `src/infra2_sdk/data/shared_env.yaml`; validate manifests with `config.EnvironmentManifest.from_model(Settings).validate_offline()`.
   - Enforce startup config via `config.assert_env(...)` or `python -m infra2_sdk.config export|check|validate`.
3. **Bootstrap HTTP & telemetry (`infra2_sdk.service`, `infra2_sdk.telemetry`)**:
   - Call `service.init_service(app, name, ...)` once for HTTP apps or `telemetry.init_worker(name, ...)` for workers to configure JSON stdout logs, request correlation, and OTLP HTTP export.
   - Use `service.create_http_client()` / `create_async_http_client()` and `request_with_retry()` / `async_request_with_retry()` for outbound calls with `X-Request-ID` + `traceparent` propagation.
4. **Own your schema & delivery hooks (`infra2_sdk.storage`, `infra2_sdk.delivery`)**:
   - Run pre-deploy table/enum verification with `python -m infra2_sdk.storage check-schema --metadata ...`.
   - Declare a Schema v3 `runtime-hooks.manifest.json` (`delivery.RuntimeHooksManifest`) and validate/run with `python -m infra2_sdk.delivery check-hooks|run-cell|poll`.
