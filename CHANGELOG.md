# Changelog

Versions follow the compatibility rules in the [README](README.md#compatibility): additive
changes are minor releases, removing or changing a public symbol is a major release. Release
steps are in [CONTRIBUTING.md](CONTRIBUTING.md#releasing). Entries below 2.4.0 are a brief
backfill from the merged pull requests.

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
