# infra2-sdk Project Charter (init.md)

## 0. Purpose and Boundary

`infra2-sdk` is the standalone cross-repository runtime contract and explicitly invoked open-protocol adapter library. Three repositories consume this package: [`infra2`](https://github.com/wangzitian0/infra2), [`finance_report`](https://github.com/wangzitian0/finance_report), and [`truealpha`](https://github.com/wangzitian0/truealpha).

`infra2-sdk` is not an `infra2` client, not a deployment orchestrator, and not an application domain library. The SDK owns environment vocabularies, manifest schemas, release reference resolution, secret-store adapters, and thin protocol clients. `infra2` owns infrastructure orchestration, Docker Compose execution, Dokploy, host operations, and deployment schedules. Consumer applications own business logic, SQL schemas, object storage keys, and domain configuration models.

The SDK enforces a dual-mode execution invariant across all runtime environments:

- In local development or standalone CI, an application passes ordinary environment variables directly to the process.
- In managed deployments, `infra2` derives those exact same canonical environment variables from its multi-environment deployment coordinate (`deploy_v2`).
- Both execution paths run the exact same local SDK code.
- Runtime code requires zero `INFRA2_*`, Vault, or Dokploy dependencies at startup or during request execution.

## 1. Five Core Decision Principles (Why the SDK Has This Shape)

Across 170+ issues and 45 releases (`v0.1.0` through `v3.23.0`), consumer requirements and defect analyses shaped `infra2-sdk` around five core architectural decision principles.

### P1 — Contract vs. Orchestration, Zero-I/O on Import, and Thin Open-Protocol Adapters (`#19`, `#33`, `#102`)

- `import infra2_sdk` and `import infra2_sdk.runtime` perform zero network I/O, zero filesystem mutation, and zero global provider registration (`tests/test_exports.py`).
- Core runtime contracts declare zero third-party adapter dependencies in `pyproject.toml` (only `pyyaml` for offline manifest and workflow parsing, lazy-loaded where required). Consumers install only the optional extras they need (`s3`, `postgres`, `http`, `otel`, `fastapi`, `all`).
- Protocol adapters return standard upstream objects (`boto3` S3 client, `psycopg` connection probe, `httpx.Client` and `httpx.AsyncClient`, OpenTelemetry providers) rather than custom wrapper abstractions. Application policy (object keys, SQL schemas, retry budgets, trace propagation) stays in the application.
- Pure cross-repository validators (`validate_manifest_offline`, `validate_inventory`, `validate_workflow_step_contracts`) execute offline without importing consumer repositories (`#19`, `#33`).

### P2 — One Source per Fact and the Manifest v2 Supply Chain (`#16`, `#38`, `#115`, `#137`)

- Every environment variable in an application manifest (`EnvironmentManifest`) declares one `source` class (`bootstrap`, `human`, `runtime`, `release`, `decision`, `code`). The source class defines who produces the value, where truth lives, and how the value reaches the container (`#16`, `#38`).
- Shared cross-repository mechanisms (`capacity`, `ci`, `deploy`, `fastapi`, `manifests`, `observability`, `refs`, `routing`, `rules`, `runtime`, `scaffold`, `secrets`, `transport`) live in `infra2-sdk` (`v3.7.0`, `#115`). Extracting these modules from `infra2` and consumer applications ensures that all repositories share one tested implementation.
- `LlmSettings` (`src/infra2_sdk/runtime/llm.py`, `#137`) owns default models and reasoning efforts across three capability roles (`large`, `small`, `multimodal`). Consumer applications map per-role environment overrides (`LLM_LARGE_MODELS`, `LLM_SMALL_MODELS`, `LLM_MULTIMODAL_MODELS`) onto this single contract.

### P3 — 100% Consumer-Reachable Public Surface (`#102`, `#108`, `#121`, `#126`)

- Every public symbol exported by `infra2-sdk` must be imported in production code by at least one consumer (`infra2`, `finance_report`, `truealpha`) or carry an explicit documented or held justification in `tests/surface.json`.
- `tests/test_exports.py` enforces `vars(module) == __all__` on `infra2_sdk` and `infra2_sdk.runtime` (excluding private attributes and submodules). Any leftover import or undeclared symbol fails the test suite immediately.
- Internal implementation helpers and constants use a leading underscore (`_`) and never enter `__all__` (`#121`, `#126`).

### P4 — Mechanism over Prose: AST Locks and Bidirectional Documentation Guards (`#18`, `#70`, `#102`)

- `tests/test_source_environment_reads.py` parses the AST of every file under `src/infra2_sdk/` without importing the modules. It restricts constant-key environment variable reads to `RuntimeEnvKey` (9 keys) plus `FIXED_READS` (10 keys), and locks parameterized or runtime-built environment variable reads in `DYNAMIC_READS` (6 keys across `ci.py`, `deploy.py`, and `observability.py`). Direct reads of legacy aliases (`ENV`, `APP_ENV`, `IMAGE_TAG`, `SERVICE_NAME`) are forbidden outside `resolve_runtime_env` (`#102`, `#169`).
- `tests/test_release_hygiene.py` verifies release identity: the newest `CHANGELOG.md` entry and the `README.md` install pins equal the version in `pyproject.toml` (`#18`, `#70`). `release.yml` publishes only a tag on `main` that equals that version. A change under `src/infra2_sdk/` does not need a release.
- `tests/test_readme_claims.py`, `tests/test_contract_documentation.py`, and `tests/test_release_hygiene.py` execute code blocks in `README.md` and verify that the `README.md` and `init.md` module tables match the public modules and exported symbols on disk 1:1.

### P5 — Pragmatic Compatibility and Zero Dead-Weight Shims (`#102`, `#117`, `#139`)

- New fields on runtime settings dataclasses must use `kw_only=True` (`#139`) so existing positional callers do not break on upgrade.
- All consumers are internal repositories tracked by `python -m tools.sdk_usage_census --json --check` in `infra2`. Public symbols with zero callers across `infra2`, `finance_report`, and `truealpha` are deleted in minor releases rather than kept behind deprecated compatibility shims (`#102`, `#117`). Breaking changes to active callers require consumer migration first.

## 2. Package Architecture and Module Ownership

The modules in `src/infra2_sdk/` divide into two architectural layers:

### Runtime Layer (`infra2_sdk.runtime.*`, `infra2_sdk.fastapi`, `infra2_sdk.transport`)

Application processes import these modules directly at runtime:

| Module | Responsibility |
|---|---|
| `infra2_sdk.runtime.environment` | Six-tier environment vocabulary (`EnvironmentTier`), `RuntimeEnvKey` resolution, and `runtime_env_contract()` |
| `infra2_sdk.runtime.config_schema` | `EnvironmentField`, `EnvironmentManifest`, `assert_environment`, and manifest config fingerprints |
| `infra2_sdk.runtime.health` | Framework-agnostic readiness probes (`check_health`, `run_probes`, `probe_orchestrator`) and dependency status contracts |
| `infra2_sdk.runtime.http` | `HttpClientSettings`, Retry-After retry helpers (`request_with_retry`, `arequest_with_retry`), and `probe_http` |
| `infra2_sdk.runtime.llm` | `LlmSettings`, `LlmClient`, capability roles (`large`, `small`, `multimodal`), and role-based model defaults |
| `infra2_sdk.runtime.otel` | Explicit OTLP/HTTP trace, metric, and log provider bootstrap (`OtelSettings`, `PROBE_PATHS`, `configure_telemetry`) |
| `infra2_sdk.runtime.postgres` | `PostgresSettings`, `redact_postgres_error`, and `probe_postgres` |
| `infra2_sdk.runtime.s3` | `S3Settings`, `boto3` S3 client initialization, bucket reachability probe (`probe_s3`), `ensure_bucket`, and `put_object_bytes` |
| `infra2_sdk.fastapi` | `init_service` turnkey bootstrap mounting `/livez`, `/readyz`, and `/health` routes with OTLP lifecycle management |
| `infra2_sdk.transport` | Injectable `HttpTransport` and `urllib_transport` |

### Control-Plane and Delivery Contract Layer

`infra2` (as the IaC control plane and Canary consumer) and application CI/CD scripts import these modules for validation, deployment, and observability:

| Module | Responsibility |
|---|---|
| `infra2_sdk.capacity` | Capacity limits, `CapacityReading`, `RateLimitWindow`, `evaluate` thresholds, and Cloudflare analytics collection |
| `infra2_sdk.ci` | Delivery-stage vocabulary, CI gate inventory validation, workflow step inspection, cron schedule gap analysis (`ScheduleError`, `largest_gap`), and coverage ratchets |
| `infra2_sdk.deploy` | `DeployRequest` wire contracts, health poller (`poll_until_healthy`), `parse_wheel_version`, deployed release reader, and production evidence verification |
| `infra2_sdk.manifests` | Settings and runtime hooks manifest CLI driver (`--write`, `--check`, `--validate-env`, `--check-hooks`, `--run-hook`), `ManifestSpec`, `RuntimeHooksManifest`, `HookSlotSpec`, `CheckSpec`, `HookRunResult`, `StageHookPlan`, `STAGE_HOOK_PLANS`, `stage_hook_plan`, `load_hooks_manifest`, `run_hook_slot`, `all_manifests`, `check`, and `load` |
| `infra2_sdk.stage_record` | `StageRecord`: one phase record of a stage task (checks, units, uncovered, verdict) |
| `infra2_sdk.observability` | Flap hysteresis trackers (`ConsecutiveObservationState`), parameterized self-healing GitHub issue trails (`reconcile_issue_trail`), and OpenPanel query primitives |
| `infra2_sdk.refs` | Git ref resolution (`resolve_to_sha`, `resolve_pr`, `identifier_kind`, `next_patch_tag`), OCI image digest resolution, and repo URL redaction |
| `infra2_sdk.routing` | Canonical domain and routing SSOT (`CANARY_SLOT`, `PreviewStackCoordinates`, hostname and service URL resolution) |
| `infra2_sdk.rules.compose` | Pure Docker Compose file rules (`inspect_compose`) enforcing memory ceilings and rejecting bare `:latest` image references |
| `infra2_sdk.rules.imports` | Pure AST import-boundary and dependency-cycle validators (`LayerRule`, `ImportViolation`, `check_import_boundaries`, `find_dependency_cycle`) |
| `infra2_sdk.scaffold` | Microservice scaffolding engine (`python -m infra2_sdk.scaffold`) for `standalone` and `infra2` profiles |
| `infra2_sdk.secrets` | Vault KV v2 and 1Password CLI adapters, manifest-driven `SecretsResolver`, and Vault Agent template/policy renderers |

## 3. Relationship Between init.md, AGENTS.md, README.md, and CONTRIBUTING.md

Each top-level document owns one distinct concern so specifications never duplicate or drift:

| Document | Primary Audience | Owned Content |
|---|---|---|
| `init.md` | Engineers and AI agents | Architectural charter, system boundaries, dual-mode execution invariant, and the five core evolution principles (`P1`–`P5`) |
| `AGENTS.md` | AI coding agents | Always-loaded operational decision invariants, merge gate rules, and production reservation (projected from `dev_env`) |
| `README.md` | Package consumers (`infra2`, `finance_report`, `truealpha`) | Installation pins, 1:1 module reference table, runtime extras, environment contract table, compatibility notes, and executable usage examples |
| `CONTRIBUTING.md` | Package maintainers | Local verification commands, cross-repository usage census (`sdk_usage_census`), and release workflow mechanics |
