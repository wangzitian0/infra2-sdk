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
the generators for everything a deployment derives from a manifest, release identity
resolution, and capacity accounting. infra2 owns orchestration: compose files, Dokploy,
host operations, and *when* each SDK operation runs. No `INFRA2_*`, Vault, or Dokploy
value is required to load runtime settings; an application only ever reads environment
variables.

## Install

Consumers should pin a release and update deliberately:

```bash
python -m pip install \
  "infra2-sdk @ git+https://github.com/wangzitian0/infra2-sdk.git@v3.1.3"
```

## Modules

| Module | Ownership |
|---|---|
| `infra2_sdk.capacity` | Capacity limits, readings, and levels; collectors for Cloudflare analytics and 1Password rate limits |
| `infra2_sdk.cli.scaffold` | Command-line interface for microservice scaffolding |
| `infra2_sdk.ci` | Delivery-stage vocabulary and CI gate inventory validation |
| `infra2_sdk.delivery` | Environment/stage evidence and failure taxonomy |
| `infra2_sdk.deploy` | Deploy lifecycle wire contracts, dispatch client, health poller, and production evidence policies; CLI at `python -m infra2_sdk.deploy` |
| `infra2_sdk.fastapi` | Turnkey single-line FastAPI integration with /livez, /readyz, /health, and SigNoz APM |
| `infra2_sdk.manifests` | Settings manifest driver (`--write`, `--check`, `--validate-env`) wrapping application configuration models |
| `infra2_sdk.refs` | Git ref resolution, PR and release identity (`ReleaseIdentity`), remote ref queries, and credential redaction |
| `infra2_sdk.routing` | Canonical domain and routing SSOT: `AppRoutePreference`, Dokploy domain specs, and service URL resolution |
| `infra2_sdk.rules.compose` | Pure compose-file rules (memory ceilings, bare `:latest` image refs); CLI at `python -m infra2_sdk.rules` |
| `infra2_sdk.runtime.config_schema` | JSON Schema 2020-12 and environment injection manifests |
| `infra2_sdk.runtime.environment` | Canonical six-tier environment vocabulary, variable resolution, and contract definitions |
| `infra2_sdk.runtime.health` | Framework-agnostic readiness checks (`check_health`), probe runners, and dependency declarations |
| `infra2_sdk.runtime.http` | Standard httpx clients and HTTP retry semantics |
| `infra2_sdk.runtime.identity` | OCI/config/release identity, `canonical_sha256`, and OTel resource coordinates |
| `infra2_sdk.runtime.otel` | Explicit OTLP provider bootstrap, env-configured samplers, and W3C trace-context propagation |
| `infra2_sdk.runtime.postgres` | PostgreSQL DSN normalization and psycopg reachability probe |
| `infra2_sdk.runtime.s3` | Standard boto3 S3 client, probe, and safe bucket/object primitives |
| `infra2_sdk.scaffold` | Zero-compromise scaffolding engine producing compose, deployer, and entrypoint assets |
| `infra2_sdk.secrets` | Secret-store adapters (Vault KV, 1Password, Env), manifest-driven `SecretsResolver`, and Vault template renderers |
| `infra2_sdk.snapshot` | Versioned anonymized-snapshot manifest, residual-proof shape, and artifact digest verification |
| `infra2_sdk.transport` | Minimal injectable HTTP transport (`HttpTransport`, `HttpResponse`, `urllib_transport`) shared by adapters |

## Runtime extras

The core runtime contracts have no runtime dependency beyond the SDK core. Install only the
open-protocol adapters an application uses:

```bash
python -m pip install \
  'infra2-sdk[s3,postgres,otel,http] @ git+https://github.com/wangzitian0/infra2-sdk.git@v3.1.3'
# or, for a conformance canary:
python -m pip install \
  'infra2-sdk[all] @ git+https://github.com/wangzitian0/infra2-sdk.git@v3.1.3'
```

Adapter modules deliberately return standard library objects rather than infra2-specific
storage, database, HTTP, or telemetry abstractions:

- S3 returns a boto3/botocore client. Object keys, immutability, checksums, lifecycle, and
  public access remain application policy.
- PostgreSQL accepts a PostgreSQL URI and performs only a `SELECT 1` reachability probe. Failure
  evidence redacts the configured DSN and password before it enters logs or alerts.
- HTTP returns an httpx client. Provider retry budgets and idempotency policy remain with the
  caller.
- OpenTelemetry configures OTLP/HTTP providers and W3C Trace Context propagation only when
  explicitly requested. OTLP endpoints require a valid HTTP(S) host, reject embedded credentials
  and fragments, and preserve query parameters when deriving per-signal paths.

Telemetry and readiness in an application (`infra2-sdk[otel]`):

```python
from opentelemetry import trace

from infra2_sdk.runtime.otel import (
    OtelSettings,
    configure_telemetry,
    extract_trace_context,
    inject_trace_context,
)

tracer = trace.get_tracer("my-app")

# Reads OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_SERVICE_NAME, OTEL_RESOURCE_ATTRIBUTES,
# OTEL_TRACES_SAMPLER and OTEL_TRACES_SAMPLER_ARG; no endpoint (or OTEL_SDK_DISABLED) means off.
providers = configure_telemetry(OtelSettings.from_env(), set_global=True)

# Inbound: continue the caller's trace. Outbound: propagate the active span.
with tracer.start_as_current_span("handle", context=extract_trace_context(request.headers)):
    outbound_headers = inject_trace_context({"Accept": "application/json"})

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
from infra2_sdk.runtime import check_health
from infra2_sdk.runtime.http import HttpCheck

# In a readiness handler (async, any framework); DEPENDENCIES is your DependencyManifest and
# `environment` comes from environment_from_env(). A required dependency down is 503 with the
# reasons; an optional one down is 200 "degraded"; a raising probe is reported, never a 200.
status_code, body = await check_health(
    [HttpCheck("catalog", url)], manifest=DEPENDENCIES, tier=environment.tier
)
```

`run_probes()` bounds both async checks and the caller-visible lifetime of sync checks. Timed-out
sync work runs only in a daemon thread and cannot delay CLI shutdown, but Python cannot cancel its
underlying blocking I/O. Every sync adapter therefore also configures a protocol-level timeout;
custom sync checks must do the same.

```python
from infra2_sdk.runtime import RuntimeIdentity, environment_from_env
from infra2_sdk.runtime.postgres import PostgresSettings
from infra2_sdk.runtime.s3 import S3Settings, create_s3_client

runtime = environment_from_env()  # reads os.environ only when called
identity = RuntimeIdentity.from_env()  # no network or platform lookup
database = PostgresSettings.from_env()
s3 = create_s3_client(S3Settings.from_env())
```

Deployed conformance checks opt into fail-closed loading instead of inheriting local defaults:

```python
runtime = environment_from_env(required=True)
identity = RuntimeIdentity.from_env(strict=True)
```

Strict identity loading requires a real commit SHA in every deployed tier and the complete
digest/configuration/release identity in staging and production. Non-strict loaders follow
OpenTelemetry's error-handling model: malformed optional OTel values are reported as runtime
warnings and discarded instead of blocking an application that has telemetry disabled.

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
  'infra2-sdk[http] @ https://github.com/wangzitian0/infra2-sdk/releases/download/v3.1.3/infra2_sdk-3.1.3-py3-none-any.whl'
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
from fastapi import FastAPI
from infra2_sdk.fastapi import init_service
from infra2_sdk.runtime.postgres import PostgresSettings, probe_postgres

app = FastAPI(title="my-service")

# Optional database checker
db_settings = PostgresSettings.from_env()


def check_db() -> tuple[bool, str]:
    res = probe_postgres(db_settings)
    return (res.status.value == "present", res.detail or "ok")


# Turnkey initialization
init_service(app, name="my-service", db_checker=check_db)
```

This single call registers three standardized endpoints and telemetry:
- `/livez`: Liveness probe (process-only, anti-puppet rule: never touches databases or external services).
- `/readyz`: Readiness probe (evaluates `db_checker`, returns HTTP 503 if dependencies fail).
- `/health`: Aggregated health check with execution durations and status taxonomy.
- SigNoz OpenTelemetry APM: Automatic W3C TraceContext propagation and OTLP/HTTP tracing when `OTEL_EXPORTER_OTLP_ENDPOINT` is present.

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
- `requirements.txt`: Curated dependencies with exact runtime extras (`infra2-sdk[fastapi,otel,postgres]`).
- `Dockerfile`: Minimal container image recipe.
- `.env.example` (in standalone profile) or Vault Agent sidecar assets (in infra2 profile).

## Environment contract

`runtime_env_contract()` is the machine-readable source for canonical names, compatibility
aliases, and sensitivity. Canonical names prefer existing open ecosystem conventions:

| Concern | Canonical names | Compatibility aliases |
|---|---|---|
| Runtime | `ENVIRONMENT`, `OTEL_SERVICE_NAME`, `SERVICE_VERSION`, `GIT_COMMIT_SHA`, `INSTANCE_ID` | `ENV`, `APP_ENV`, `SERVICE_NAME`, `IMAGE_TAG` |
| PostgreSQL | `DATABASE_URL`, `DATABASE_CONNECT_TIMEOUT_SECONDS` | — |
| S3 | `OBJECT_STORAGE_PROTOCOL=s3`, `S3_BUCKET`, `AWS_ENDPOINT_URL_S3`, `AWS_REGION`, standard AWS credentials | `OBJECT_STORAGE_DRIVER`, `S3_ENDPOINT`, `S3_REGION`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` |
| Telemetry | `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_SDK_DISABLED`, `OTEL_METRIC_EXPORT_INTERVAL`, `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG` | — |

Canonical and alias values may coexist only when equal. Conflicts fail before clients are
created, and secret values never appear in errors. Missing OTLP configuration disables
telemetry without affecting application startup. `OTEL_SDK_DISABLED=true` takes precedence over
the endpoint and ignores it completely. Missing S3 credentials leaves boto3's standard credential
chain intact.

Missing S3 region and addressing-style values also remain unset so boto3 can use its standard
profile, workload-identity, and service defaults. S3-compatible deployments that require path
addressing set `S3_ADDRESSING_STYLE=path` explicitly. When bucket creation is explicitly allowed,
`ensure_bucket` uses the region resolved by the boto3 client if no region was set directly.

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
- `to_environment_tier` and `to_deploy_type` provide canonical bridging mappings across `EnvironmentTier` and `DeployType`.
- `CommandRunner` protocol is exported from `infra2_sdk.refs` for subprocess runner injection.

### Anonymized snapshot trust boundary

`infra2_sdk.snapshot` validates a closed v1 wire shape and proves that local
artifact bytes match the manifest's non-zero size and SHA-256 digest. Shape and
digest validation **does not attest** that the declared producer actually ran
the anonymizer. Before any restore or deploy side effect, infrastructure must
independently authorize the declared repository, source SHA, workflow run, and
artifact provenance. Dumping, anonymization, storage, Vault, host access, and
deployment remain outside the SDK.

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
