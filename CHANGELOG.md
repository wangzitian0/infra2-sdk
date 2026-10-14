# Changelog

Versions follow the compatibility rules in the [README](README.md#compatibility). Release
steps are in [CONTRIBUTING.md](CONTRIBUTING.md#releasing). Entries below 2.4.0 are a brief
backfill from the merged pull requests.

## 3.5.1 - 2026-10-14

### Changed

- Document `require_issued_identity` on `OtelSettings.from_env` and `init_service` in the README ([#108](https://github.com/wangzitian0/infra2-sdk/issues/108)).

### Fixed

- Replace the wall-clock `elapsed < 0.1` check in `test_sync_probe_timeout_does_not_delay_cli_event_loop_shutdown` with deterministic `threading.Event` coordination ([#99](https://github.com/wangzitian0/infra2-sdk/issues/99)).

### Removed

- Remove 12 public symbols with zero production and test callers across `infra2`, `finance_report`, and `truealpha` ([#108](https://github.com/wangzitian0/infra2-sdk/issues/108)):
  - `cli.scaffold.main`, the `infra2_sdk.cli` package, and the `infra2-scaffold` / `infra2-sdk` console script entry points;
  - `deploy.build_deploy_request`, `deploy.derive_release_evidence`, `deploy.main`, `deploy.dispatch_main`, and `deploy.deploy_health_main`;
  - `routing.RouteEndpoint` and `routing.AppRoutePreference`;
  - `runtime.environment.to_deploy_type` and `runtime.environment.APP_OWNED_TIERS`;
  - `runtime.health.health_response`;
  - `runtime.otel.OtelSettings.from_identity`.
- Remove `RuntimeIdentity` from `infra2_sdk.runtime.__all__`; `infra2_sdk.runtime.identity.RuntimeIdentity` remains the canonical symbol ([#108](https://github.com/wangzitian0/infra2-sdk/issues/108)).

## 3.5.0 - 2026-10-14

### Added

- Add `infra2_sdk.deploy.validate_evidence_urls(request, *, expected_repository=None)`. It checks that the evidence URLs are `github.com` URLs of one repository. With `expected_repository`, a request of another repository raises `ValueError` ([#105](https://github.com/wangzitian0/infra2-sdk/issues/105)).
- Add `infra2_sdk.runtime.otel.REQUIRED_IDENTITY_ATTRIBUTES`: `deployment.environment.name`, `infra.service.id`, `service.version` and `infra.iac.ref` ([#105](https://github.com/wangzitian0/infra2-sdk/issues/105)).
- Add the keyword `require_issued_identity` (default `False`) to `OtelSettings.from_env` and `init_service`. With an active OTLP endpoint, it requires `OTEL_SERVICE_NAME` and the four identity attributes, ignores `ENVIRONMENT`, `ENV` and `APP_ENV`, and takes the tier from `deployment.environment.name` ([#105](https://github.com/wangzitian0/infra2-sdk/issues/105)).
- Add `infra2_sdk.runtime.environment.is_preview_alias`, and `canonical_json_bytes` and `canonical_sha256` in the private module `infra2_sdk._wire` ([#105](https://github.com/wangzitian0/infra2-sdk/issues/105)).

### Changed

- Accept the underscore preview aliases `pr_12`, `branch_main`, `commit_1ab32d5`, `tag_v1_2_3` and `canary_preview`. They give the same tier and display name as the hyphen form ([#105](https://github.com/wangzitian0/infra2-sdk/issues/105)).
- Reconcile a staging behavior tier with a preview `deployment.environment.name` in `OtelSettings.from_env`. The result is tier `preview`. A staging tier with the name `production` still raises `ValueError` ([#105](https://github.com/wangzitian0/infra2-sdk/issues/105)).
- Make `resolve_app_hostname` return `<slug>-<alias>.<domain>` for a preview alias in `tier` or `slot_id` (`report-pr-5`, `report-branch-main`). An integer `slot_id` is a PR number. Other slots keep `<slug>-preview-<slot>`. Before, `slot_id="pr-5"` gave `report-preview-pr-5` ([#105](https://github.com/wangzitian0/infra2-sdk/issues/105), [#91](https://github.com/wangzitian0/infra2-sdk/issues/91)).
- Make the canary host `<slug>-canary-preview.<domain>`. Before, it was `canary-preview.<domain>` without the slug. The census of 2026-10-14 finds no consumer call with a canary tier or slot ([#105](https://github.com/wangzitian0/infra2-sdk/issues/105)).
- Use one canonical JSON hash function in `runtime.identity` and `runtime.config_schema` ([#105](https://github.com/wangzitian0/infra2-sdk/issues/105)).
- Generate `mem_limit` in the scaffold `compose.yaml`, so `inspect_compose` accepts it. Generate `compose_path` and `public_route = PublicRouteFacet()` in the scaffold `deploy.py` ([#105](https://github.com/wangzitian0/infra2-sdk/issues/105)).

## 3.4.0 - 2026-10-14

### Added

- Add the keyword `default_service_name` to `OtelSettings.from_env`. It is the last source of the service name ([#102](https://github.com/wangzitian0/infra2-sdk/issues/102)).
- Add the keyword-only field `sdk_disabled` to `OtelSettings`. It is true when `OTEL_SDK_DISABLED` is true, so a disabled SDK differs from a missing endpoint ([#102](https://github.com/wangzitian0/infra2-sdk/issues/102)).

### Changed

- Resolve the tier, the resource attributes, the service name, the service version and the instance id in one private function. `OtelSettings.from_env`, `RuntimeIdentity.from_env` and `init_service` use it ([#102](https://github.com/wangzitian0/infra2-sdk/issues/102)).
- Set one rule for the service name: `OTEL_SERVICE_NAME`, then `service.name` in `OTEL_RESOURCE_ATTRIBUTES`, then the default name, then `unknown_service` (`OtelSettings` only). `init_service` now uses `service.name` before its `name` argument. `RuntimeIdentity.from_env` now uses `service.name` and raises `ValueError` only when no name exists ([#102](https://github.com/wangzitian0/infra2-sdk/issues/102)).
- Count a blank or padded `service.name`, `service.version` or `service.instance.id` attribute as missing, or strip it, as for an environment variable. Before, the blank text won over the next source ([#102](https://github.com/wangzitian0/infra2-sdk/issues/102)).
- Make `init_service` raise `ValueError("name must not be blank")` for an empty or blank name, before the app changes ([#102](https://github.com/wangzitian0/infra2-sdk/issues/102)).
- Change the default of `strict_otel` in `init_service` from `False` to `True`. A strict `init_service` raises `ValueError` when the tier is missing, unknown or in conflict, when `OTEL_RESOURCE_ATTRIBUTES` is malformed or disagrees with the tier, when `OTEL_SDK_DISABLED`, `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG` or `OTEL_METRIC_EXPORT_INTERVAL` is invalid, or when two aliases of one variable differ. Pass `strict_otel=False` for local runs and for services that must never stop on telemetry ([#102](https://github.com/wangzitian0/infra2-sdk/issues/102)).
- Make strict `init_service` without an endpoint run the same validation as with an endpoint. Before, it logged the missing-endpoint line and returned, so a missing tier or a malformed value stayed hidden ([#102](https://github.com/wangzitian0/infra2-sdk/issues/102)).
- Check the telemetry settings in `init_service` before the app changes. A strict error leaves no marker and no probe route, so a retry meets the real error and not `RuntimeError` ([#102](https://github.com/wangzitian0/infra2-sdk/issues/102)).
- Name the variable at fault in the strict errors of `OtelSettings.from_env`: `invalid OTEL_SDK_DISABLED: ...`, `invalid OTEL_METRIC_EXPORT_INTERVAL: ...`, `invalid deployment.environment.name: ...`. `init_service` adds the way out, `pass strict_otel=False to log the problem and start` ([#102](https://github.com/wangzitian0/infra2-sdk/issues/102)).
- Change non-strict `init_service`: a malformed `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_SDK_DISABLED` or sampler variable gives the Python `RuntimeWarning` also when no endpoint is set. With `OTEL_SDK_DISABLED=true` and no endpoint a deployed tier logs the disabled line at DEBUG, no longer the missing-endpoint WARNING. The level of the missing-endpoint line follows the tier that `from_env` resolved, also when it inferred the tier from `deployment.environment.name`. Every other problem stays a log record of the logger `infra2_sdk.fastapi` ([#102](https://github.com/wangzitian0/infra2-sdk/issues/102)).
- When an environment has several faults, the fault that `OtelSettings.from_env` and `RuntimeIdentity.from_env` report first can differ from 3.3.0. An environment with one fault gives the same result ([#102](https://github.com/wangzitian0/infra2-sdk/issues/102)).
- Generate `strict_otel=False` in the `app.py` of the `standalone` scaffold profile, with a comment that a deployment must also set a real `ENV` ([#102](https://github.com/wangzitian0/infra2-sdk/issues/102)).
- `runtime_env_contract()` lists 14 variables (was 34) and has `contract_version` 2. A receiver that requires version 1 rejects it. `RuntimeEnvKey` has 14 members.
- Generate the README environment table from `runtime_env_contract()` with `scripts/render_env_contract.py`. A test compares the README block and the script output byte for byte. The wheel does not ship the script.
- Lock the exported names of `infra2_sdk` and `infra2_sdk.runtime` in `tests/surface.json`. Each name has a reason: a consumer, a README section, or a recorded decision.
- The scaffolded `app.py` builds `PostgresSettings(dsn=...)` from `DATABASE_URL` and raises `RuntimeError: DATABASE_URL is required` when it is unset or empty.
- `examples/runtime_check.py` uses `check_health`, a `DependencyCheck` and `probe_http`.

### Fixed

- Fix the fail-open start-up of `init_service`. With no tier set, a deployed service without an OTLP endpoint logged at DEBUG and started ([#102](https://github.com/wangzitian0/infra2-sdk/issues/102)).
- Fix `init_service` ignoring `service.name` in `OTEL_RESOURCE_ATTRIBUTES` when `OTEL_SERVICE_NAME` is unset ([#102](https://github.com/wangzitian0/infra2-sdk/issues/102)).

### Removed

- Remove the 18 public names that no consumer uses. The census of 2026-10-13 finds no reference in infra2, finance_report or truealpha, in any file type ([#102](https://github.com/wangzitian0/infra2-sdk/issues/102)):
  - `capacity.LEVELS` and `capacity.onepassword_capacity`;
  - `delivery.PREVIEW_RELEVANT_STAGES`;
  - `refs.resolve_release_identity` (use `refs.resolve_image_ref` and `refs.resolve_image_digest`) and `refs.verify_runtime_identity` (no replacement);
  - `routing.resolve_dokploy_domains`;
  - `runtime.config_schema.settings_json_schema`;
  - `runtime.health.assert_required_dependencies`; use `check_health`;
  - `runtime.http.retryable_request` and `runtime.http.parse_retry_after` (no replacement), `runtime.http.HttpCheck` (use `probe_http`);
  - `runtime.identity.runtime_identity_fingerprint`;
  - `runtime.otel.inject_trace_context` and `runtime.otel.extract_trace_context`; use `opentelemetry.propagate`;
  - `runtime.postgres.PostgresCheck` and `runtime.s3.S3Check`; wrap `probe_postgres` or `probe_s3` in a `DependencyCheck`;
  - `secrets.EnvBackend`;
  - `snapshot.verify_snapshot_artifact`.
- Remove the names that only the removed code used: `capacity.OnePasswordCapacityReport`, `capacity.Runner`, `refs.ReleaseIdentity` (no replacement), `routing.DokployDomainSpec`, `runtime.health.DependencyUnavailableError`, `runtime.environment.env_float`, `runtime.environment.env_bool` and `secrets.ReadOnlyBackendError` ([#102](https://github.com/wangzitian0/infra2-sdk/issues/102)).
- Remove `deploy.DeployStatus` and the module `infra2_sdk.snapshot` with its 7 public names. No consumer uses them ([#102](https://github.com/wangzitian0/infra2-sdk/issues/102)).
- Remove `S3Settings.from_env`, `HttpClientSettings.from_env` and `PostgresSettings.from_env`. All consumers build the settings with explicit values ([#102](https://github.com/wangzitian0/infra2-sdk/issues/102)).
- Remove 20 members of `RuntimeEnvKey` and the 28 variable names they resolve from `runtime_env_contract()`: seven `HTTP_*` names, 18 S3 names (aliases included) and three database names ([#102](https://github.com/wangzitian0/infra2-sdk/issues/102)).
- Remove the unused alias `SERVICE_NAME` of `OTEL_SERVICE_NAME`. The SDK no longer reads it ([#102](https://github.com/wangzitian0/infra2-sdk/issues/102)).
- Remove 31 names from `__all__` of `infra2_sdk` (18) and `infra2_sdk.runtime` (13), and `__version__` from `infra2_sdk.__all__`. They stay defined in their modules. Remove the attributes `infra2_sdk.version` and `infra2_sdk.PackageNotFoundError` ([#102](https://github.com/wangzitian0/infra2-sdk/issues/102)).

### Compatibility note

Version 3.4.0 removes public names and environment variables that no known consumer uses, and changes the default of `init_service(strict_otel=...)` to `True`. The basis is a usage census of infra2, finance_report and truealpha (production and test code, all file types, 2026-10-13 and 2026-10-14). It found no use of a removed name and no reliance on the old default: infra2 `platform/12.alerting` has passed `strict_otel=False` explicitly since infra2#1240, and `platform/30.todo` passes `strict_otel=True`. The README Compatibility section states the rule. `runtime_env_contract()` has `contract_version` 2. A consumer outside the census must read the Removed list before it moves to 3.4.0.

## 3.3.0 - 2026-10-12

### Added

- Add the keywords `strict_otel`, `log_level`, and `check_timeout_seconds` to `init_service` ([#89](https://github.com/wangzitian0/infra2-sdk/issues/89), [#93](https://github.com/wangzitian0/infra2-sdk/pull/93)).
- Log a warning that names `OTEL_EXPORTER_OTLP_ENDPOINT` when `init_service` runs in a deployed tier (preview, staging, production) without it ([#89](https://github.com/wangzitian0/infra2-sdk/issues/89), [#93](https://github.com/wangzitian0/infra2-sdk/pull/93)).
- Write `vault-policy.hcl` in the `infra2` scaffold profile ([#89](https://github.com/wangzitian0/infra2-sdk/issues/89), [#93](https://github.com/wangzitian0/infra2-sdk/pull/93)).
- Raise `RuntimeError` on a second `init_service` call for one app, because that call duplicated `/livez` and shut the shared providers down twice ([#89](https://github.com/wangzitian0/infra2-sdk/issues/89), [#93](https://github.com/wangzitian0/infra2-sdk/pull/93)).
- Add the OpenTelemetry packages to the `fastapi` extra ([#89](https://github.com/wangzitian0/infra2-sdk/issues/89), [#93](https://github.com/wangzitian0/infra2-sdk/pull/93)).

### Changed

- Build `/readyz` and `/health` of `init_service` with `runtime.health.check_health`, so each check entry has `status`, `detail`, `duration_ms`, and `required` ([#89](https://github.com/wangzitian0/infra2-sdk/issues/89), [#93](https://github.com/wangzitian0/infra2-sdk/pull/93)).
- `/health` of `init_service` now returns the `runtime.health` report: check status `failed` is now `absent`, each reason starts with the check name, and `service` stays ([#89](https://github.com/wangzitian0/infra2-sdk/issues/89), [#93](https://github.com/wangzitian0/infra2-sdk/pull/93)).
- Raise the `fastapi` extra lower bound to 0.142, the first release that ships `fastapi.telemetry` ([#89](https://github.com/wangzitian0/infra2-sdk/issues/89), [#93](https://github.com/wangzitian0/infra2-sdk/pull/93)).
- Render the scaffold `secrets.ctmpl` with `infra2_sdk.secrets.render_agent_template` from a declared manifest, and remove the `fastapi` line from the generated `requirements.txt` ([#89](https://github.com/wangzitian0/infra2-sdk/issues/89), [#93](https://github.com/wangzitian0/infra2-sdk/pull/93)).
- Run the dispatch and deploy-health CLIs as `python -m infra2_sdk.deploy dispatch` and `python -m infra2_sdk.deploy deploy-health`, not as `python -m infra2_sdk.dispatch` and `python -m infra2_sdk.deploy_health` ([#90](https://github.com/wangzitian0/infra2-sdk/issues/90), [#92](https://github.com/wangzitian0/infra2-sdk/pull/92)).
- Define `JSON_SCHEMA_DIALECT` once in `infra2_sdk._wire`, so `config_schema`, `health`, `identity`, and `snapshot` import it and `infra2_sdk.runtime.health.JSON_SCHEMA_DIALECT` stays importable ([#90](https://github.com/wangzitian0/infra2-sdk/issues/90), [#92](https://github.com/wangzitian0/infra2-sdk/pull/92)).

### Fixed

- Fix `/readyz` and `/health` that answered 200 when an async `db_checker` returned `False`, and fail closed on exceptions and timeouts ([#89](https://github.com/wangzitian0/infra2-sdk/issues/89), [#93](https://github.com/wangzitian0/infra2-sdk/pull/93)).
- Fix the OpenTelemetry shutdown that never ran on Starlette 1.x, because the SDK now stops the providers with the application lifespan ([#89](https://github.com/wangzitian0/infra2-sdk/issues/89), [#93](https://github.com/wangzitian0/infra2-sdk/pull/93)).
- Fix the 3.1.3 regression where `init_service` left the root logger at WARNING and dropped INFO logs ([#89](https://github.com/wangzitian0/infra2-sdk/issues/89), [#93](https://github.com/wangzitian0/infra2-sdk/pull/93)).
- Fix `GET /openapi.json`, which answered 500 for every app that called `init_service` because the `-> Response` annotation of the route stayed a `ForwardRef` ([#89](https://github.com/wangzitian0/infra2-sdk/issues/89), [#93](https://github.com/wangzitian0/infra2-sdk/pull/93)).
- Fix the scaffold vault-agent healthcheck for `--db none` with the `infra2` profile, which now tests that `.env` exists, not that it has content, when the manifest declares no secret ([#89](https://github.com/wangzitian0/infra2-sdk/issues/89), [#93](https://github.com/wangzitian0/infra2-sdk/pull/93)).

### Removed

- Remove the six compatibility shim modules `infra2_sdk.deploy_health`, `infra2_sdk.dispatch`, `infra2_sdk.release`, `infra2_sdk.runtime.environ`, `infra2_sdk.runtime.dependencies`, and `infra2_sdk.runtime.probes`; import `infra2_sdk.deploy`, `infra2_sdk.refs`, `infra2_sdk.runtime.environment`, and `infra2_sdk.runtime.health` instead ([#90](https://github.com/wangzitian0/infra2-sdk/issues/90), [#92](https://github.com/wangzitian0/infra2-sdk/pull/92)).
- Remove `routing.LEGACY_CANARY_PR` and its mapping of the slot ids `999` and `pr-999` to the canary host, so these slot ids now resolve as ordinary preview slots ([#90](https://github.com/wangzitian0/infra2-sdk/issues/90), [#92](https://github.com/wangzitian0/infra2-sdk/pull/92)).
- Remove the keyword hooks `api_client_factory`, `dispatch_fn`, `http_get_factory`, and `poll_fn` from `deploy.dispatch_main` and `deploy.deploy_health_main`, because only the removed shims used them ([#90](https://github.com/wangzitian0/infra2-sdk/issues/90), [#92](https://github.com/wangzitian0/infra2-sdk/pull/92)).
- Remove the private alias `infra2_sdk.runtime.postgres._redact_error`; use `redact_postgres_error` instead ([#94](https://github.com/wangzitian0/infra2-sdk/issues/94), [#95](https://github.com/wangzitian0/infra2-sdk/pull/95)).
- Remove the 3.1.0 promises that the code never kept (Prometheus metrics, structured logging, graceful shutdown handling, and a generated `pyproject.toml`) and the SigNoz APM wording ([#89](https://github.com/wangzitian0/infra2-sdk/issues/89), [#93](https://github.com/wangzitian0/infra2-sdk/pull/93)).
- Remove the dead `FastAPIInstrumentor` branch ([#89](https://github.com/wangzitian0/infra2-sdk/issues/89), [#93](https://github.com/wangzitian0/infra2-sdk/pull/93)).
- Remove the scaffold `generic` template, `--template generic`, and `scaffold.generate_secrets_ctmpl` ([#89](https://github.com/wangzitian0/infra2-sdk/issues/89), [#93](https://github.com/wangzitian0/infra2-sdk/pull/93)).

### Compatibility note

Version 3.3.0 removes the six compatibility shims and the private aliases that the Compatibility section of the README announced for removal after consumers moved; all known consumers have moved.

## 3.1.3 - 2026-10-12

### Fixed

- Wire FastAPI `init_service` OpenTelemetry setup directly to `infra2_sdk.runtime.otel.configure_telemetry` to enable OTLP log exporting and prevent silent telemetry loss ([#86](https://github.com/wangzitian0/infra2-sdk/issues/86), [#87](https://github.com/wangzitian0/infra2-sdk/pull/87)).
- Register application shutdown lifecycle handler in FastAPI `init_service` to unbind and flush OpenTelemetry logging handlers cleanly ([#86](https://github.com/wangzitian0/infra2-sdk/issues/86), [#87](https://github.com/wangzitian0/infra2-sdk/pull/87)).

## 3.1.2 - 2026-10-12

### Added

- Add `--profile {infra2,standalone}` support in microservice scaffolder; `standalone` generates pure 12-factor `.env.example` and Compose without Vault sidecars or external networks ([#84](https://github.com/wangzitian0/infra2-sdk/issues/84)).
- Wire PostgreSQL reachability probe into scaffolded FastAPI application when `--db postgres` is requested ([#84](https://github.com/wangzitian0/infra2-sdk/issues/84)).
- Register `infra2-sdk` script entry point alongside `infra2-scaffold` and support forwarded `scaffold` subcommand dispatch ([#84](https://github.com/wangzitian0/infra2-sdk/issues/84)).
- Add `fastapi` extra to CI wheel smoke matrix and offline `smoke_fastapi` test in `wheel_smoke.py` ([#84](https://github.com/wangzitian0/infra2-sdk/issues/84)).
- Curate dependency extras in scaffolded `requirements.txt` (`infra2-sdk[fastapi,otel,postgres]>=3.1.2`) ([#84](https://github.com/wangzitian0/infra2-sdk/issues/84)).
- Add user documentation in README for `init_service` and `infra2-scaffold` ([#84](https://github.com/wangzitian0/infra2-sdk/issues/84)).

## 3.1.1 - 2026-10-09

### Fixed

- Deduplicate string and integer parsing in `snapshot.py` and internal runtime modules using canonical `_wire.py` helpers ([#80](https://github.com/wangzitian0/infra2-sdk/issues/80), [#81](https://github.com/wangzitian0/infra2-sdk/pull/81)).
- Deduplicate evidence policy parsing in `deploy.py` through shared payload helper ([#80](https://github.com/wangzitian0/infra2-sdk/issues/80), [#81](https://github.com/wangzitian0/infra2-sdk/pull/81)).
- Deduplicate container service inspection in compose rule checker with `inspect_service_resource_limits` ([#80](https://github.com/wangzitian0/infra2-sdk/issues/80), [#81](https://github.com/wangzitian0/infra2-sdk/pull/81)).
- Prevent false deprecation warnings in `routing.py` when passing canonical `CANARY_SLOT` ([#80](https://github.com/wangzitian0/infra2-sdk/issues/80), [#81](https://github.com/wangzitian0/infra2-sdk/pull/81)).
- Remove obsolete tuple unpacking protocol from `OnePasswordCapacityReport` in `capacity.py` ([#80](https://github.com/wangzitian0/infra2-sdk/issues/80), [#81](https://github.com/wangzitian0/infra2-sdk/pull/81)).
- Remove dead private aliases `_ls_remote_rows`, `_redact_repo`, `_signal_endpoint`, `_string`, `_integer`, and `_require_exact_fields` ([#80](https://github.com/wangzitian0/infra2-sdk/issues/80), [#81](https://github.com/wangzitian0/infra2-sdk/pull/81)).

## 3.1.0 - 2026-10-09

### Added

- Introduce `infra2-sdk scaffold` CLI command for AST-compliant microservice scaffolding ([#75](https://github.com/wangzitian0/infra2-sdk/issues/75), [#78](https://github.com/wangzitian0/infra2-sdk/pull/78)).
- Introduce `init_service` 1-line FastAPI initializer with OpenTelemetry, Prometheus metrics, structured logging, and health routes ([#75](https://github.com/wangzitian0/infra2-sdk/issues/75), [#78](https://github.com/wangzitian0/infra2-sdk/pull/78)).
- Add template generators for `compose.yaml`, `vault-agent.hcl`, `vault-policy.hcl`, `deploy.py`, and `pyproject.toml` ([#75](https://github.com/wangzitian0/infra2-sdk/issues/75), [#78](https://github.com/wangzitian0/infra2-sdk/pull/78)).

## 3.0.2 - 2026-10-06

### Fixed

- Harden external subprocess calls with explicit `timeout=30.0s` and structured timeout error handling in `onepassword_capacity` and `OnePasswordBackend._op` ([#73](https://github.com/wangzitian0/infra2-sdk/issues/73)).
- Add statement timeout configuration to `PostgresSettings` and `probe_postgres` ([#73](https://github.com/wangzitian0/infra2-sdk/issues/73)).
- Close HTTP connection pool and error streams in `github_api_client` and `urllib_transport` ([#73](https://github.com/wangzitian0/infra2-sdk/issues/73)).
- Bound concurrent sync probe worker threads in `_run_sync_probe` using an asynchronous semaphore ([#73](https://github.com/wangzitian0/infra2-sdk/issues/73)).
- Fail closed on malformed production evidence policy files during release evidence derivation ([#73](https://github.com/wangzitian0/infra2-sdk/issues/73)).
- Fix test assertions and eliminate tautological checks in `test_deploy.py` and `test_compat_shims.py` ([#73](https://github.com/wangzitian0/infra2-sdk/issues/73)).

## 3.0.1 - 2026-10-06

### Fixed

- Eradicate residual internal imports of backwards-compatibility shims across runtime modules (`config_schema`, `http`, `identity`) and test suites, ensuring pure unidirectional SSOT dependency flow ([#71](https://github.com/wangzitian0/infra2-sdk/issues/71)).

## 3.0.0 - 2026-10-06

### Removed

- Eradicate legacy `infra2_sdk.images` module and `data/platform_images.yaml` ([#66](https://github.com/wangzitian0/infra2-sdk/issues/66)).
- Eradicate legacy `RuntimeIdentity.to_otel_resource_attributes()` method ([#66](https://github.com/wangzitian0/infra2-sdk/issues/66)).
- Prune obsolete tests and consolidate legacy test suites into canonical domain suites ([#68](https://github.com/wangzitian0/infra2-sdk/issues/68)).

### Changed

- Consolidate secondary modules into canonical primary modules with backwards-compatible shims ([#66](https://github.com/wangzitian0/infra2-sdk/issues/66), [#68](https://github.com/wangzitian0/infra2-sdk/issues/68)):
  - `infra2_sdk.deploy`: Directly implements dispatch and deploy health polling (shims: `dispatch`, `deploy_health`).
  - `infra2_sdk.refs`: Directly implements release identity resolution (shim: `release`).
  - `infra2_sdk.runtime.environment`: Directly implements environment vocabulary and variables (shim: `runtime.environ`).
  - `infra2_sdk.runtime.health`: Directly implements dependency declarations, probes, and health check runner (shims: `runtime.dependencies`, `runtime.probes`).
- Streamline documentation, removing ancient v0.2 and v2.0 eradication historical notes ([#68](https://github.com/wangzitian0/infra2-sdk/issues/68)).
- Update module table and package version to 3.0.0.

## 2.7.0 - 2026-10-06

### Added

- Export canonical submodules and capability symbols in `infra2_sdk.__init__` and `infra2_sdk.runtime.__init__` (`__all__`), enabling natural Python introspection and autocompletion ([#64](https://github.com/wangzitian0/infra2-sdk/issues/64)).
- Export public `redact_postgres_error(detail, settings)` in `infra2_sdk.runtime.postgres` with `_redact_error` retained as backwards-compatible alias ([#63](https://github.com/wangzitian0/infra2-sdk/issues/63)).
- Add `tests/test_exports.py` guarding export completeness and import isolation without third-party extras.

## 2.6.0 - 2026-10-06

### Changed

- Deprecate `routing.LEGACY_CANARY_PR` (PR 999) mapping as infra2 and consumers have migrated to canonical `CANARY_SLOT` (`canary-preview`).
- Standardize runtime schema manifests and routing resolution on canonical identifiers.

## 2.5.0 - 2026-10-05

### Added

- `deploy.verify_production_evidence`: Verifies production deploy requests against GitHub API actions and pull requests.
- `deploy.derive_release_evidence`: Derives release evidence from GitHub workflow runs and merged pull requests.
- `deploy.canonical_json`: Serializes `DeployRequest` to deterministic canonical wire JSON.
- `deploy.fetch_production_evidence_policy`: Fetches the application production evidence contract from GitHub.
- CLI entrypoints for deploy protocol contracts and operations:
  - `python -m infra2_sdk.deploy`: Build requests, derive evidence, and verify production evidence.
  - `python -m infra2_sdk.dispatch`: Dispatch requests to infra2 receiver and wait for completion.
  - `python -m infra2_sdk.deploy_health`: Poll HTTP health endpoints until healthy with version checks.

## 2.4.1 - 2026-10-05

### Fixed

- `runtime.environment.to_environment_tier` now resolves the SDK's own canary slot name
  (`"canary-preview"`) to `EnvironmentTier.PREVIEW` instead of raising `unknown environment`.
  infra2 issues that name as the canary slot's `deployment.environment.name`, so every consumer
  that derives the tier from the issued identity (for example `OtelSettings.from_env`) failed on
  the canary slot ([#54](https://github.com/wangzitian0/infra2-sdk/issues/54)).
- `CANARY_SLOT` is now defined once in `runtime.environment`; `routing.CANARY_SLOT` (and the
  top-level export) re-exports the same object. No public name changed.

## 2.4.0 - 2026-10-05

Completes the observability base package, publishes the private helpers consumers imported,
and cleans up residue ([#52](https://github.com/wangzitian0/infra2-sdk/issues/52)).

### Added

- `runtime.otel.extract_trace_context(headers)`: inbound W3C Trace Context to an OpenTelemetry
  context (case-insensitive header names; a missing or malformed `traceparent` is a new root
  trace, not an error). `inject_trace_context` gains a keyword-only `context=` argument.
- Sampler configuration through the standard `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG`
  (`always_on`, `always_off`, `traceidratio`, `parentbased_*`): keyword-only
  `OtelSettings.traces_sampler` / `traces_sampler_arg`, loaded by `OtelSettings.from_env`
  (strict mode raises on an invalid value; otherwise it is reported and discarded), and
  registered in `runtime_env_contract()` (additive, contract version unchanged).
- `configure_telemetry(..., capture_logs=True)` and `TelemetryProviders.logging_handler`: with
  `set_global=True` an OpenTelemetry logging handler bound to this call's `LoggerProvider` is
  attached to the root logger explicitly (not through the instrumentor's version-dependent
  auto-instrumentation) and removed by `shutdown()`.
- `runtime.health`: `check_health(checks, required=... | manifest=..., tier=...)` and
  `health_response(results, required=...)` return `(status_code, body)` with the
  `{"status": "healthy"|"degraded"|"unhealthy", "checks": {...}, "reasons": [...]}` body of
  infra2's health-check contract. A required dependency that is absent, raised, timed out or was
  never probed is 503 with reasons; exceptions are never turned into a 200. Also exported from
  `infra2_sdk.runtime`.
- Public `runtime.otel.signal_endpoint`, `refs.ls_remote_rows` (its `runner` now defaults to
  `subprocess.run`) and `refs.redact_repo`.
- `py.typed` marker, shipped in the wheel and sdist: type checkers now use the SDK's
  annotations. A consumer that configured `ignore_missing_imports` for `infra2_sdk` because the
  package was untyped should re-run its type check when it upgrades.
- Release-hygiene tests: README version pins equal the package version, the CHANGELOG has an
  entry for it, every public module is in the README table, and `py.typed` ships.

### Changed

- `configure_telemetry(set_global=True)` is idempotent: while the first installation is active
  a second call returns it (a `RuntimeWarning` if the settings differ) instead of building a
  second set of exporters and attaching a second handler. A global provider that was already set
  by another component is reported with a `RuntimeWarning`.
- The tracer provider's sampler is now built from `OtelSettings` only; `configure_telemetry`
  no longer lets the OpenTelemetry SDK read `OTEL_TRACES_SAMPLER` from the ambient process
  environment. `OtelSettings.from_env()` reads it, so the usual path is unchanged. No consumer
  calls `configure_telemetry` today.
- `infra2_sdk.capacity`, `release` and `secrets` import the transport from `infra2_sdk.transport`.
- README: install examples pin the current release, the module table lists every public module.

### Deprecated (removed in 3.0.0)

- The `infra2_sdk.images` module: nothing consumes it and its catalog is not kept in sync with
  infra2's compose pins. Importing it or calling its functions emits a `DeprecationWarning`.
- `RuntimeIdentity.to_otel_resource_attributes()` (deprecated since 2.1.0): now warns. Use
  `to_standard_otel_resource_attributes()`.
- `runtime.otel._signal_endpoint`, `refs._ls_remote_rows` and `refs._redact_repo`: plain aliases
  of the public names, kept so existing imports (and patches) keep working. Migrate to
  `signal_endpoint`, `ls_remote_rows` and `redact_repo`; the aliases are removed once no
  consumer imports them.

### Removed

- The `minio` and `mc` entries of `data/platform_images.yaml`: infra2 no longer runs MinIO
  (object storage is RustFS) and no correct digest exists to replace them.
- The private `infra2_sdk._transport` compatibility shim (no consumer imports it).

### Release process

- The v2.3.1 `Release` run failed at `gh release create` ("a release with the same tag name
  already exists") because the release had been created by hand while the workflow was still
  running, so its assets were built locally rather than by the workflow. Let the tag push create
  the release; see [CONTRIBUTING.md](CONTRIBUTING.md#releasing).

### Planned for 3.0.0

- Remove `infra2_sdk.images`, `RuntimeIdentity.to_otel_resource_attributes()` and the private
  aliases listed above.

## 2.3.1 - 2026-09-25

- `infra2_sdk.routing`: canonical domain, routing and Dokploy domain specifications; `build_deploy_request`.

## 2.3.0 - 2026-09-25

- `infra2_sdk.images` platform image catalog; compose rules CLI (`python -m infra2_sdk.rules`).

## 2.2.0 - 2026-09-25

- Compose resource-limit and image-pin rules extracted into `infra2_sdk.rules.compose`.

## 2.1.1 - 2026-09-23

- Optional secret schema support; production `assert` statements replaced by exceptions.

## 2.1.0 - 2026-09-23

- Environment tier resolution unified; `infra2_sdk.transport` exported.

## 2.0.1 - 2026-09-23

- Runtime type hints restored; critical probe signals passed through.

## 2.0.0 - 2026-09-23

- Deprecated v1 symbols removed (listed in the README's Compatibility section).
