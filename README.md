# infra2-sdk

Versioned runtime contracts and explicitly invoked protocol adapters shared by
[`infra2`](https://github.com/wangzitian0/infra2),
[`finance_report`](https://github.com/wangzitian0/finance_report), and
[`truealpha`](https://github.com/wangzitian0/truealpha).

The SDK is a long-lived application dependency, not an infra2 client. It owns a stable
environment-variable vocabulary, validation, serialization, and thin adapters over open
runtime protocols. A standalone process passes ordinary environment variables; infra2 may
derive the same variables from its multi-environment deployment coordinate. Both paths execute
the same local SDK code.

The SDK owns the contracts and the adapters over open protocols: the environment manifest
and its source classes, the secret-store adapters (Vault KV v2 HTTP API, the 1Password CLI),
the generators for everything a deployment derives from a manifest, release reference
resolution (tag to commit, tag to image digest), and capacity accounting. infra2 owns
orchestration: compose files, Dokploy, host operations, and *when* each SDK operation runs.
No `INFRA2_*`, Vault, or Dokploy value is required to load runtime settings; an application
only ever reads environment variables.

## Install

Consumers should pin a release and update deliberately:

```bash
python -m pip install \
  "infra2-sdk @ git+https://github.com/wangzitian0/infra2-sdk.git@v3.3.0"
```

## Modules

| Module | Ownership |
|---|---|
| `infra2_sdk.capacity` | Capacity limits, readings, and levels; the collector for Cloudflare analytics |
| `infra2_sdk.cli.scaffold` | Command-line interface for microservice scaffolding |
| `infra2_sdk.ci` | Delivery-stage vocabulary and CI gate inventory validation |
| `infra2_sdk.delivery` | Environment/stage evidence and failure taxonomy |
| `infra2_sdk.deploy` | Deploy lifecycle wire contracts, dispatch client, health poller, and production evidence policies; CLI at `python -m infra2_sdk.deploy` |
| `infra2_sdk.fastapi` | `init_service`: /livez, /readyz and /health routes for a FastAPI app, plus OTLP telemetry bootstrap |
| `infra2_sdk.manifests` | Settings manifest driver (`--write`, `--check`, `--validate-env`) wrapping application configuration models |
| `infra2_sdk.refs` | Git ref resolution, PR heads, registry image digests, remote ref queries, and credential redaction |
| `infra2_sdk.routing` | Canonical domain and routing SSOT: `AppRoutePreference`, hostname resolution, and service URL resolution |
| `infra2_sdk.rules.compose` | Pure compose-file rules (memory ceilings, bare `:latest` image refs); CLI at `python -m infra2_sdk.rules` |
| `infra2_sdk.runtime.config_schema` | JSON Schema 2020-12 and environment injection manifests |
| `infra2_sdk.runtime.environment` | Canonical six-tier environment vocabulary, variable resolution, and contract definitions |
| `infra2_sdk.runtime.health` | Framework-agnostic readiness checks (`check_health`), probe runners, and dependency declarations |
| `infra2_sdk.runtime.http` | Standard httpx clients and the HTTP reachability probe |
| `infra2_sdk.runtime.identity` | OCI/config/release identity, `canonical_sha256`, and OTel resource coordinates |
| `infra2_sdk.runtime.otel` | Explicit OTLP provider bootstrap and env-configured samplers |
| `infra2_sdk.runtime.postgres` | PostgreSQL DSN normalization and psycopg reachability probe |
| `infra2_sdk.runtime.s3` | Standard boto3 S3 client, probe, and safe bucket/object primitives |
| `infra2_sdk.scaffold` | Zero-compromise scaffolding engine producing compose, deployer, and entrypoint assets |
| `infra2_sdk.secrets` | Secret-store adapters (Vault KV, 1Password), manifest-driven `SecretsResolver`, and Vault template renderers |
| `infra2_sdk.transport` | Minimal injectable HTTP transport (`HttpTransport`, `HttpResponse`, `urllib_transport`) shared by adapters |

### Core runtime entrypoints

| Capability | Canonical entrypoint | Primary module | Description |
|---|---|---|---|
| Service bootstrap | `init_service` | `infra2_sdk.fastapi` | Mounts `/livez`, `/readyz`, `/health` routes and configures OpenTelemetry providers |
| Health probes | `check_health`, `run_probes` | `infra2_sdk.runtime.health` | Framework-agnostic readiness evaluation across declared dependency probes |
| HTTP client | `create_http_client`, `probe_http` | `infra2_sdk.runtime.http` | Standard httpx client with connection limits, timeouts, and connect retries, plus a reachability probe |
| PostgreSQL | `normalize_postgres_dsn`, `probe_postgres` | `infra2_sdk.runtime.postgres` | Psycopg reachability probe with credential redaction and DSN normalization |
| S3 storage | `create_s3_client`, `probe_s3`, `ensure_bucket` | `infra2_sdk.runtime.s3` | Boto3 client initialization, bucket reachability probe, and idempotent bucket creation |
| Environment & identity | `environment_from_env` | `infra2_sdk.runtime.environment` | Canonical six-tier environment resolution and contract validation |
| Identity verification | `RuntimeIdentity` | `infra2_sdk.runtime.identity` | Strict 40-hex commit SHA and deployment release attribute validation |
| Secret manifests | `render_agent_template`, `render_agent_policy` | `infra2_sdk.secrets` | Manifest-driven Vault Agent Consul template and HCL policy generation |

## Runtime extras

The core runtime contracts have no runtime dependency beyond the SDK core. Install only the
open-protocol adapters an application uses:

```bash
python -m pip install \
  'infra2-sdk[s3,postgres,otel,http] @ git+https://github.com/wangzitian0/infra2-sdk.git@v3.3.0'
# or, for a conformance canary:
python -m pip install \
  'infra2-sdk[all] @ git+https://github.com/wangzitian0/infra2-sdk.git@v3.3.0'
```

Adapter modules deliberately return standard library objects rather than infra2-specific
storage, database, HTTP, or telemetry abstractions:

- S3 returns a boto3/botocore client. Object keys, immutability, checksums, lifecycle, and
  public access remain application policy.
- PostgreSQL accepts a PostgreSQL URI and performs only a `SELECT 1` reachability probe. Failure
  evidence redacts the configured DSN and password before it enters logs or alerts.
- HTTP returns an httpx client. Provider retry budgets and idempotency policy remain with the
  caller.
- OpenTelemetry configures OTLP/HTTP providers only when explicitly requested. OTLP endpoints
  require a valid HTTP(S) host, reject embedded credentials and fragments, and preserve query
  parameters when deriving per-signal paths. Trace-context propagation stays with the
  application: use `opentelemetry.propagate.inject` and `extract`.

Telemetry and readiness in an application (`infra2-sdk[otel]`):

```python
from opentelemetry import trace

from infra2_sdk.runtime.otel import OtelSettings, configure_telemetry

tracer = trace.get_tracer("my-app")

# Reads OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_SERVICE_NAME, OTEL_RESOURCE_ATTRIBUTES,
# OTEL_TRACES_SAMPLER and OTEL_TRACES_SAMPLER_ARG; no endpoint (or OTEL_SDK_DISABLED) means off.
providers = configure_telemetry(OtelSettings.from_env(), set_global=True)

with tracer.start_as_current_span("handle"):
    ...

providers.shutdown()  # on exit: flushes exporters and detaches the logging handler
```

`set_global=True` installs the providers as the OpenTelemetry globals (the API allows this once
per process), adds `otelTraceID`/`otelSpanID` to log records, and attaches an OpenTelemetry
logging handler bound to the SDK's `LoggerProvider` to the root logger (`capture_logs=False`
opts out of the handler). Calling it again returns the active installation. Without
`set_global`, nothing outside the returned providers changes. The sampler comes from the
settings (standard `OTEL_TRACES_SAMPLER` names; `OTEL_TRACES_SAMPLER_ARG` for the ratio
samplers); ambient `os.environ` is only read by `from_env()`.

```python
from infra2_sdk.runtime import ProbeResult, check_health
from infra2_sdk.runtime.http import probe_http


class CatalogCheck:  # a DependencyCheck: a `name` and a `probe()` that returns a ProbeResult
    name = "catalog"

    async def probe(self) -> ProbeResult:
        return await probe_http(url, name=self.name)


# In a readiness handler (async, any framework); DEPENDENCIES is your DependencyManifest and
# `environment` comes from environment_from_env(). A required dependency down is 503 with the
# reasons; an optional one down is 200 "degraded"; a raising probe is reported, never a 200.
status_code, body = await check_health(
    [CatalogCheck()], manifest=DEPENDENCIES, tier=environment.tier
)
```

`run_probes()` bounds both async checks and the caller-visible lifetime of sync checks. Timed-out
sync work runs only in a daemon thread and cannot delay CLI shutdown, but Python cannot cancel its
underlying blocking I/O. Every sync adapter therefore also configures a protocol-level timeout;
custom sync checks must do the same.

Settings of the S3, PostgreSQL and HTTP adapters take explicit values. The application owns
the variable names and the defaults. The SDK reads no environment variable for these settings.

```python
import os

from infra2_sdk.runtime import RuntimeIdentity, environment_from_env
from infra2_sdk.runtime.postgres import PostgresSettings
from infra2_sdk.runtime.s3 import S3Settings, create_s3_client

runtime = environment_from_env()  # reads os.environ only when called
identity = RuntimeIdentity.from_env()  # no network or platform lookup
database = PostgresSettings(dsn=os.environ["DATABASE_URL"])
s3 = create_s3_client(S3Settings(bucket="my-bucket", endpoint_url=os.environ["S3_ENDPOINT"]))
```

Deployed conformance checks opt into fail-closed loading instead of inheriting local defaults:

```python
runtime = environment_from_env(required=True)
identity = RuntimeIdentity.from_env(strict=True)
```

Strict identity loading requires a real commit SHA in every deployed tier and the complete
digest/configuration/release identity in staging and production. Note that `RuntimeIdentity`
validates that `GIT_COMMIT_SHA` (when not `'unknown'`) is a 40-character lowercase hexadecimal
string (`^[0-9a-f]{40}$`) in both strict and non-strict modes. Deployments injecting release tags
(e.g. `v0.1.74`) or short SHAs cannot construct `RuntimeIdentity` with that commit ref and should
leave `GIT_COMMIT_SHA` unset or rely on platform-managed `ServiceIdentity` attributes. Non-strict
loaders follow OpenTelemetry's error-handling model: malformed optional OTel resource attributes
are reported as runtime warnings and discarded instead of blocking an application that has
telemetry disabled.

## Start an independent app

Copy [examples/runtime_check.py](examples/runtime_check.py) to `runtime_check.py`
in your own app's root directory, then run the commands below from that directory. It
loads a required environment, validates runtime identity, probes an HTTP dependency,
and returns nonzero if the required dependency is missing or unhealthy. It uses only
public SDK imports and ordinary environment variables; no sibling checkout or
platform credentials are needed.

Install the published wheel in a fresh Python 3.11+ environment:

```bash
python -m venv .venv
.venv/bin/python -m pip install \
  'infra2-sdk[http] @ https://github.com/wangzitian0/infra2-sdk/releases/download/v3.3.0/infra2_sdk-3.3.0-py3-none-any.whl'
```

For a local connectivity exercise, start this server in another terminal:

```bash
python -m http.server 8765 --bind 127.0.0.1
```

Then run:

```bash
ENVIRONMENT=local_dev OTEL_SERVICE_NAME=my-app \
  CATALOG_HEALTH_URL=http://127.0.0.1:8765/ \
  .venv/bin/python runtime_check.py
```

The command prints JSON with `"ready": true`. Remove `CATALOG_HEALTH_URL`, or use an
unhealthy endpoint, and it prints `"ready": false` and exits 1. This exercises HTTP
reachability only. In a real app, replace `catalog` and its required tiers with
your own dependency policy and call this boundary from startup/readiness. Keep
business validation, routes, storage layout, and deployment policy in the app.
Deployed tiers must receive genuine release identity from their release system;
the example does not invent production identity to bypass strict validation.

The HTTP wheel smoke runs this example against a local server and checks both
success and failure paths on every PR, without installing the SDK source tree.

## Turnkey FastAPI integration

The SDK provides single-line compliance with operational observability standards:

```python
import os

from fastapi import FastAPI
from infra2_sdk.fastapi import init_service
from infra2_sdk.runtime.postgres import PostgresSettings, probe_postgres

app = FastAPI(title="my-service")

# Optional database checker
db_settings = PostgresSettings(dsn=os.environ["DATABASE_URL"])


def check_db() -> tuple[bool, str]:
    res = probe_postgres(db_settings)
    return (res.status.value == "present", res.detail or "ok")


# Turnkey initialization
init_service(app, name="my-service", db_checker=check_db)
```

This single call registers three standardized endpoints and starts telemetry
(`pip install 'infra2-sdk[fastapi]'` includes the OpenTelemetry packages):
- `/livez`: Liveness probe (process-only, anti-puppet rule: never touches databases or external services).
- `/readyz`: Readiness probe. It runs `db_checker` and answers HTTP 503 with `reasons` when the check fails.
- `/health`: The `infra2_sdk.runtime.health` report (`status`, `checks`, `reasons`) plus `service`. A failed database check gives `unhealthy` and HTTP 503.
- OpenTelemetry: when `OTEL_EXPORTER_OTLP_ENDPOINT` is set, one OTLP bootstrap exports traces, metrics and logs. It stops with the application lifespan. FastAPI 0.142 or later emits the request spans itself. A deployed tier (preview, staging, production) without the endpoint logs a warning that names the variable.

`db_checker` returns `bool` or `(bool, detail)` and may be sync or async. A false result, an exception, or a call longer than `check_timeout_seconds` (default 10) counts as a failure. Sync checkers run in a worker thread. `strict_otel=True` raises on invalid telemetry settings instead of logging a warning.

Before OpenTelemetry attaches its logging handler, `init_service` calls `logging.basicConfig(level=log_level)` (default `INFO`). The call changes nothing when the root logger already has a handler. Pass `log_level=None` to skip it. The SDK serves no `/metrics` endpoint.

## Microservice scaffolder

Generate standard microservice skeletons with a single command:

```bash
# Generate pure 12-factor standalone service (.env, compose without Vault)
infra2-scaffold my-service --profile standalone --db postgres --port 8000

# Or generate Dokploy/Vault sidecar service for the infra2 cluster
infra2-scaffold my-service --profile infra2 --project apps --db postgres --port 8000
```

Both `infra2-scaffold` and `infra2-sdk scaffold` CLI invocations are supported.

Generated assets include:
- `compose.yaml`: Hardened Docker Compose declaration with explicit memory ceilings and health check intervals.
- `app.py`: FastAPI application with `init_service` and database probe wiring.
- `entrypoint.sh`: Container bootstrap script applying migrations safely.
- `requirements.txt`: Curated dependencies with exact runtime extras (`infra2-sdk[fastapi,otel,postgres]`). The `infra2-sdk` lower bound is the `major.minor.patch` release of the SDK that generated the file.
- `Dockerfile`: Minimal container image recipe.
- `.env.example` (in standalone profile) or `deploy.py` and the Vault assets (in infra2 profile): `secrets.ctmpl` and `vault-policy.hcl` are rendered by `infra2_sdk.secrets` from one manifest, `vault-agent.hcl` configures the sidecar.

## Environment contract

`runtime_env_contract()` is the machine-readable source for canonical names, compatibility
aliases, and sensitivity. Canonical names prefer existing open ecosystem conventions. The table
below is generated from it. Do not edit it by hand. After a change of the contract, replace the
text between the two markers with the output of `python scripts/render_env_contract.py`. The
test suite fails when the block is stale.

<!-- env-contract:begin (generated by scripts/render_env_contract.py; do not edit) -->
| Canonical name | Compatibility aliases | Sensitive |
|---|---|---|
| `ENVIRONMENT` | `ENV`, `APP_ENV` | no |
| `OTEL_SERVICE_NAME` | none | no |
| `SERVICE_VERSION` | `IMAGE_TAG` | no |
| `GIT_COMMIT_SHA` | none | no |
| `IMAGE_DIGEST` | none | no |
| `CONFIGURATION_SHA256` | none | no |
| `RELEASE_ID` | none | no |
| `INSTANCE_ID` | none | no |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | none | no |
| `OTEL_RESOURCE_ATTRIBUTES` | none | no |
| `OTEL_SDK_DISABLED` | none | no |
| `OTEL_METRIC_EXPORT_INTERVAL` | none | no |
| `OTEL_TRACES_SAMPLER` | none | no |
| `OTEL_TRACES_SAMPLER_ARG` | none | no |
<!-- env-contract:end -->

Canonical and alias values may coexist only when equal. Conflicts fail before clients are
created, and secret values never appear in errors. Missing OTLP configuration disables
telemetry without affecting application startup. `OTEL_SDK_DISABLED=true` takes precedence over
the endpoint and ignores it completely.

The table lists the variables that `environment_from_env`, `RuntimeIdentity.from_env` and
`OtelSettings.from_env` resolve. The settings of the S3, PostgreSQL and HTTP adapters take
explicit values, so no row of the table belongs to them. Two more fixed names exist outside the
table. `environment_from_env` reads `GITHUB_ACTIONS`. `VaultKvBackend.from_environ` reads
`VAULT_ADDR`, `VAULT_TOKEN`, `VAULT_ROLE_ID` and `VAULT_SECRET_ID` from the mapping you pass.
Other names come from the caller: the manifest that you pass to `validate_env`, and the token
variable that a CLI option names.

`ENVIRONMENT` has two dimensions. `RuntimeEnvironment.tier` controls behavior using the six
portable tiers. `RuntimeEnvironment.name` preserves the deployment display identity. Therefore
deploy_v2 aliases such as `pr-42`, `branch-main`, `commit-1ab32d5`, and `tag-v1-2-3` all resolve
to tier `preview` as compatibility inputs. New producers set `ENVIRONMENT=preview` and carry an
arbitrary display identity through the standard
`OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=<name>` attribute. The SDK does not impose
an infra2 naming grammar on that display identity.

## Configuration supply chain

Manifest contract version 2 gives every field a *source class*: who may legitimately
produce its value. Everything else is derived from that answer.

| Source | Produced by | Truth lives in | Reaches the container via | Written to the store |
|---|---|---|---|---|
| `bootstrap` | a person, once | 1Password | never (provisioning only) | never |
| `human` | a person, from an external issuer | 1Password item `project/{env|shared}/service` | Vault Agent render | by `SecretsResolver.sync_human()` on deploy, only when different |
| `runtime` | the deployment | Vault `secret/project/env/service` | Vault Agent render | by `ensure_runtime()` once; `mirror_to_1password` copies back for humans |
| `release` | the release request | the tag (git + registry) | compose environment | never |
| `decision` | a reviewed human decision | a file in the app repository | compose environment | never |
| `code` | a default in the settings model | the model | the default | never |

`provided_by="project/service:KEY"` references another service's value;
`composed_from="…{KEY}…"` builds a value from such references. `empty_ok` is the only way a
rendered variable may be empty; `ci.validate_manifest_offline()` rejects manifests that
would otherwise fail late, and it runs without any infrastructure.

### deploy_v2 boundary

deploy_v2's `(service, type, version_ref, iac_ref)` remains a deployment-control coordinate,
not an application environment contract. A deployment producer derives only runtime results:

- deploy type/alias -> `ENVIRONMENT`;
- resolved full application SHA -> `GIT_COMMIT_SHA`;
- immutable image ref -> `SERVICE_VERSION`;
- OCI digest -> `IMAGE_DIGEST` when available.

`version_ref`, `iac_ref`, `staging_validated`, and `code_reviewed` are never required runtime
variables. A non-infra2 deployment can provide the same canonical variables directly.

## Compatibility

- Semantic versions describe the public Python and serialized JSON contracts.
- Additive fields and enum values require a minor release.
- Removing or changing an existing field requires a major release.
- New runtime dataclass fields are keyword-only so additive releases do not rebind existing
  positional arguments.
- Receivers must require a JSON integer `contract_version` and reject booleans or unsupported
  values before side effects.
- Repository submodules are development workspace pointers, not package dependencies.
- Importing any runtime module performs no network I/O and mutates no global provider state.
- Since 2.4.0 the package ships a `py.typed` marker, so type checkers use its annotations.
- In v3.0.0, these legacy symbols were removed:
  - The `infra2_sdk.images` module and `data/platform_images.yaml`;
  - `RuntimeIdentity.to_otel_resource_attributes()`.
- In v3.1.1, these private aliases were removed. Use the public name:
  `runtime.otel._signal_endpoint` is `signal_endpoint`, `refs._ls_remote_rows` is
  `ls_remote_rows`, `refs._redact_repo` is `redact_repo`.
- In v2.4.0, the private module `infra2_sdk._transport` was removed; import `infra2_sdk.transport`.
- In v3.3.0, the six compatibility shim modules were removed. Import the canonical module:
  - `infra2_sdk.dispatch` and `infra2_sdk.deploy_health`: use `infra2_sdk.deploy`. The CLIs are
    `python -m infra2_sdk.deploy dispatch` and `python -m infra2_sdk.deploy deploy-health`;
  - `infra2_sdk.release`: use `infra2_sdk.refs`;
  - `infra2_sdk.runtime.environ`: use `infra2_sdk.runtime.environment`;
  - `infra2_sdk.runtime.dependencies` and `infra2_sdk.runtime.probes`: use
    `infra2_sdk.runtime.health`.
- In v3.3.0, `routing.LEGACY_CANARY_PR` and its `999` and `pr-999` slot mapping were removed.
  Those slot ids now resolve as ordinary preview slots. For the canary host, pass `tier="canary"`,
  `is_canary=True`, or `slot_id=CANARY_SLOT`.
- In v3.4.0, the SDK removed public names that no consumer uses. The basis is the same as in
  v3.3.0: a census of infra2, finance_report and truealpha found no use of them. Replacements
  are in brackets.
  - `capacity.LEVELS`, `capacity.onepassword_capacity` and `capacity.OnePasswordCapacityReport`;
  - `delivery.PREVIEW_RELEVANT_STAGES`;
  - `deploy.DeployStatus` (no replacement: build the record you need in your own code;
    `DeployState` stays);
  - `refs.resolve_release_identity` and `refs.verify_runtime_identity` (use
    `refs.resolve_image_ref` and `refs.resolve_image_digest`);
  - `refs.ReleaseIdentity` (no replacement: build the record you need in your own code);
  - `routing.resolve_dokploy_domains` and `routing.DokployDomainSpec`;
  - `runtime.config_schema.settings_json_schema`;
  - `runtime.environment.env_float` and `runtime.environment.env_bool`;
  - `runtime.health.assert_required_dependencies` and `runtime.health.DependencyUnavailableError`
    (use `check_health`);
  - `runtime.http.retryable_request`, `runtime.http.parse_retry_after` and
    `runtime.http.HttpCheck` (use `probe_http`);
  - `runtime.identity.runtime_identity_fingerprint`;
  - `runtime.otel.inject_trace_context` and `runtime.otel.extract_trace_context` (use
    `opentelemetry.propagate.inject` and `extract`);
  - `runtime.postgres.PostgresCheck` and `runtime.s3.S3Check` (use `probe_postgres` and
    `probe_s3`);
  - `secrets.EnvBackend` and `secrets.ReadOnlyBackendError`;
  - the module `infra2_sdk.snapshot`, with `AnonymizedSnapshotManifest`, `ResidualScanProof`,
    `ResidualScanStatus`, `SNAPSHOT_MANIFEST_VERSION`, `SnapshotArtifact`,
    `SnapshotArtifactFormat`, `SnapshotProducer` and `verify_snapshot_artifact` (no replacement:
    infra2 removed the last user, `tools/sync_anonymized_staging.sh`, on 2026-10-12. A project
    that still needs the manifest shape pins `infra2-sdk<3.4`).
- In v3.4.0, `S3Settings.from_env`, `HttpClientSettings.from_env` and `PostgresSettings.from_env`
  were removed. Build the settings with explicit values. The variables that only these methods
  read left `RuntimeEnvKey` and `runtime_env_contract()`:
  - HTTP: `HTTP_TIMEOUT_SECONDS`, `HTTP_CONNECT_TIMEOUT_SECONDS`, `HTTP_MAX_CONNECTIONS`,
    `HTTP_MAX_KEEPALIVE_CONNECTIONS`, `HTTP_CONNECT_RETRIES`, `HTTP_USER_AGENT`,
    `HTTP_FOLLOW_REDIRECTS`;
  - S3: `OBJECT_STORAGE_PROTOCOL` (alias `OBJECT_STORAGE_DRIVER`), `S3_BUCKET`,
    `AWS_ENDPOINT_URL_S3` (aliases `S3_ENDPOINT_URL`, `S3_ENDPOINT`), `AWS_REGION` (aliases
    `AWS_DEFAULT_REGION`, `S3_REGION`), `AWS_ACCESS_KEY_ID` (alias `S3_ACCESS_KEY`),
    `AWS_SECRET_ACCESS_KEY` (alias `S3_SECRET_KEY`), `AWS_SESSION_TOKEN` (alias
    `S3_SESSION_TOKEN`), `S3_CONNECT_TIMEOUT_SECONDS`, `S3_READ_TIMEOUT_SECONDS`,
    `S3_ADDRESSING_STYLE`;
  - database: `DATABASE_URL`, `DATABASE_CONNECT_TIMEOUT_SECONDS`,
    `DATABASE_STATEMENT_TIMEOUT_SECONDS`.
- In v3.4.0, the SDK no longer reads `SERVICE_NAME`, the unused alias of `OTEL_SERVICE_NAME`.
  `ENV`, `APP_ENV` and `IMAGE_TAG` stay.
- In v3.4.0, names that no consumer imports and no recorded decision holds left `__all__` of
  `infra2_sdk` and `infra2_sdk.runtime`. They stay defined in their modules. Import them from the
  module, for example `from infra2_sdk.deploy import DeployState`. `tests/surface.json` records
  the reason for each remaining export: a consumer imports it, the README documents it, or a
  recorded decision holds it.
- `to_environment_tier` and `to_deploy_type` provide canonical bridging mappings across `EnvironmentTier` and `DeployType`.
- `CommandRunner` protocol is exported from `infra2_sdk.refs` for subprocess runner injection.

## Development

Read [CONTRIBUTING.md](CONTRIBUTING.md) for ownership, compatibility, and validation.
Named manifest override tables must resolve to mappings; missing or invalid tables fail
before generating a file, so source classifications cannot silently disappear.

```bash
python -m pip install -e '.[dev]'
ruff check .
pytest --cov
python -m build
```
