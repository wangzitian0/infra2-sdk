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
  "infra2-sdk @ git+https://github.com/wangzitian0/infra2-sdk.git@v3.16.0"
```

## Modules

| Module | Ownership |
|---|---|
| `infra2_sdk.capacity` | Capacity limits and readings, `evaluate` (a level per item: ok, warn, exceeded or unknown), and the collector for Cloudflare analytics |
| `infra2_sdk.ci` | Delivery-stage vocabulary, CI gate inventory validation, workflow step inspection, coverage ratchets, and GitHub Actions helpers |
| `infra2_sdk.delivery` | Environment/stage evidence and failure taxonomy |
| `infra2_sdk.deploy` | Deploy lifecycle wire contracts, dispatch client, health poller, deployed release identity reader, and production evidence policies |
| `infra2_sdk.fastapi` | `init_service`: /livez, /readyz and /health routes for a FastAPI app, plus OTLP telemetry bootstrap |
| `infra2_sdk.manifests` | Settings manifest driver (`--write`, `--check`, `--validate-env`) and boot environment assertions wrapping application configuration models |
| `infra2_sdk.observability` | Flap hysteresis trackers, scheduled peer workflow liveness evaluation, self-healing GitHub issue trails, and OpenPanel export query & funnel summary primitives |
| `infra2_sdk.refs` | Git ref resolution, PR heads, registry image digests, remote ref queries, commit ancestry checks, and credential redaction |
| `infra2_sdk.routing` | Canonical domain and routing SSOT: `CANARY_SLOT`, hostname resolution, and service URL resolution |
| `infra2_sdk.rules.compose` | Pure compose-file rules (memory ceilings, bare `:latest` image refs); CLI at `python -m infra2_sdk.rules` |
| `infra2_sdk.runtime.config_schema` | Environment field contracts, injection manifests, and configuration fingerprints |
| `infra2_sdk.runtime.environment` | Canonical six-tier environment vocabulary, variable resolution, and contract definitions |
| `infra2_sdk.runtime.health` | Framework-agnostic readiness checks (`check_health`), probe runners, and dependency declarations |
| `infra2_sdk.runtime.http` | Standard httpx clients and the HTTP reachability probe |
| `infra2_sdk.runtime.llm` | Large language model runtime configuration contracts and role-based model defaults |
| `infra2_sdk.runtime.otel` | Explicit OTLP provider bootstrap and env-configured samplers |
| `infra2_sdk.runtime.postgres` | PostgreSQL DSN normalization and psycopg reachability probe |
| `infra2_sdk.runtime.s3` | Standard boto3 S3 client, probe, and safe bucket/object primitives |
| `infra2_sdk.scaffold` | Zero-compromise scaffolding engine producing compose, deployer, and entrypoint assets; CLI at `python -m infra2_sdk.scaffold` |
| `infra2_sdk.secrets` | Secret-store adapters (Vault KV, 1Password), manifest-driven `SecretsResolver`, Vault template renderers, and AppRole policy naming |
| `infra2_sdk.transport` | Minimal injectable HTTP transport (`HttpTransport`, `HttpResponse`, `urllib_transport`) |

### Core runtime entrypoints

| Capability | Canonical entrypoint | Primary module | Description |
|---|---|---|---|
| Service bootstrap | `init_service` | `infra2_sdk.fastapi` | Mounts `/livez`, `/readyz`, `/health` routes and configures OpenTelemetry providers |
| Health probes | `check_health`, `run_probes` | `infra2_sdk.runtime.health` | Framework-agnostic readiness evaluation across declared dependency probes |
| HTTP client | `create_http_client`, `probe_http` | `infra2_sdk.runtime.http` | Standard httpx client with connection limits, timeouts, and connect retries, plus a reachability probe |
| PostgreSQL | `normalize_postgres_dsn`, `probe_postgres` | `infra2_sdk.runtime.postgres` | Psycopg reachability probe with credential redaction and DSN normalization |
| S3 storage | `create_s3_client`, `probe_s3`, `ensure_bucket` | `infra2_sdk.runtime.s3` | Boto3 client initialization, bucket reachability probe, and idempotent bucket creation |
| Environment & identity | `environment_from_env` | `infra2_sdk.runtime.environment` | Canonical six-tier environment resolution and contract validation |
| Secret manifests | `render_agent_template`, `render_agent_policy` | `infra2_sdk.secrets` | Manifest-driven Vault Agent Consul template and HCL policy generation |

## Runtime extras

The core runtime contracts have no runtime dependency beyond the SDK core. Install only the
open-protocol adapters an application uses:

```bash
python -m pip install \
  'infra2-sdk[s3,postgres,otel,http] @ git+https://github.com/wangzitian0/infra2-sdk.git@v3.16.0'
# or, for a conformance canary:
python -m pip install \
  'infra2-sdk[all] @ git+https://github.com/wangzitian0/infra2-sdk.git@v3.16.0'
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
  application: use `opentelemetry.propagate.inject` and `extract`. `extract` reads a plain
  `dict` case-sensitively, so `Traceparent` is not found. Pass lower-case keys or a
  case-insensitive mapping such as the Starlette `request.headers`.

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

Pass `require_issued_identity=True` to `OtelSettings.from_env` or `init_service` when a
deployment platform issues identity through `OTEL_RESOURCE_ATTRIBUTES`. When
`require_issued_identity=True`, the SDK ignores `ENVIRONMENT`, `ENV`, and `APP_ENV` and derives the
runtime tier from `deployment.environment.name`. When an OTLP endpoint is active, it requires
`OTEL_SERVICE_NAME` and the four `REQUIRED_IDENTITY_ATTRIBUTES` (`deployment.environment.name`,
`infra.service.id`, `service.version`, `infra.iac.ref`).

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
boto3 may: when an S3 setting stays unset, `create_s3_client` passes `None` and boto3 applies
its own provider chain. That chain reads variables such as `AWS_ACCESS_KEY_ID`,
`AWS_DEFAULT_REGION` and `AWS_ENDPOINT_URL_S3`.

```python
import os

from infra2_sdk.runtime import environment_from_env
from infra2_sdk.runtime.otel import OtelSettings
from infra2_sdk.runtime.postgres import PostgresSettings
from infra2_sdk.runtime.s3 import S3Settings, create_s3_client

runtime = environment_from_env()  # reads os.environ only when called
telemetry = OtelSettings.from_env()  # no network or platform lookup
database = PostgresSettings(dsn=os.environ["DATABASE_URL"])
s3 = create_s3_client(S3Settings(bucket="my-bucket", endpoint_url=os.environ["S3_ENDPOINT"]))
```

Deployed conformance checks opt into fail-closed loading instead of inheriting local defaults:

```python
runtime = environment_from_env(required=True)
telemetry = OtelSettings.from_env(strict=True, require_issued_identity=True)
```

Non-strict loaders follow OpenTelemetry's error-handling model: malformed optional OTel resource
attributes are reported as runtime warnings and discarded instead of blocking an application that
has telemetry disabled. This holds for `from_env(strict=False)`. `init_service` is strict by
default; see "Turnkey FastAPI integration".

## Start an independent app

Copy [examples/runtime_check.py](examples/runtime_check.py) to `runtime_check.py`
in your own app's root directory, then run the commands below from that directory. It
loads a required environment, validates telemetry settings, probes an HTTP dependency,
and returns nonzero if the required dependency is missing or unhealthy. It uses only
public SDK imports and ordinary environment variables; no sibling checkout or
platform credentials are needed.

Install the published wheel in a fresh Python 3.11+ environment:

```bash
python -m venv .venv
.venv/bin/python -m pip install \
  'infra2-sdk[http] @ https://github.com/wangzitian0/infra2-sdk/releases/download/v3.16.0/infra2_sdk-3.16.0-py3-none-any.whl'
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


# Turnkey initialization. strict_otel defaults to True: ENVIRONMENT (or ENV, APP_ENV) must name
# a tier. On a laptop, set ENVIRONMENT=local_dev or pass strict_otel=False.
init_service(app, name="my-service", db_checker=check_db)
```

This single call registers three standardized endpoints and starts telemetry
(`pip install 'infra2-sdk[fastapi]'` includes the OpenTelemetry packages):
- `/livez`: Liveness probe (process-only, anti-puppet rule: never touches databases or external services).
- `/readyz`: Readiness probe. It runs `db_checker` and answers HTTP 503 with `reasons` when the check fails.
- `/health`: The `infra2_sdk.runtime.health` report (`status`, `checks`, `reasons`) plus `service`. A failed database check gives `unhealthy` and HTTP 503.
- OpenTelemetry: when `OTEL_EXPORTER_OTLP_ENDPOINT` is set, one OTLP bootstrap exports traces, metrics and logs. It stops with the application lifespan. FastAPI 0.142 or later emits the request spans itself. A deployed tier (preview, staging, production) without the endpoint logs a warning that names the variable, unless `OTEL_SDK_DISABLED=true`.

`db_checker` returns `bool` or `(bool, detail)` and may be sync or async. A false result, an exception, or a call longer than `check_timeout_seconds` (default 10) counts as a failure. Sync checkers run in a worker thread.

`name` must not be blank. The telemetry service name is `OTEL_SERVICE_NAME`, then `service.name` in `OTEL_RESOURCE_ATTRIBUTES`, then `name`. A blank variable or attribute value counts as missing.

`strict_otel` defaults to `True`, so `init_service` fails closed. A deployed service needs `ENVIRONMENT` (or `ENV`, `APP_ENV`). A laptop sets `ENVIRONMENT=local_dev` or passes `strict_otel=False`. In strict mode `init_service` raises `ValueError`, before it changes the app, when:

- the tier is missing or unknown;
- `OTEL_RESOURCE_ATTRIBUTES` is malformed, or its `deployment.environment.name` disagrees with the tier;
- `OTEL_SDK_DISABLED`, `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG` or `OTEL_METRIC_EXPORT_INTERVAL` is invalid (the sampler argument counts only for a ratio sampler);
- two aliases of one variable hold different values. `init_service` reads two groups of aliases: the tier variables (`ENVIRONMENT`, `ENV`, `APP_ENV`), and `SERVICE_VERSION` with `IMAGE_TAG`. The contract has no other alias.

An `OTEL_EXPORTER_OTLP_ENDPOINT` that is not a valid http(s) URL also stops the start, unless `OTEL_SDK_DISABLED=true`. These checks run with or without an endpoint. `OTEL_SDK_DISABLED=true` does not skip the four checks above. `enable_otel=False` skips every telemetry check; the blank-name check always runs. The SDK does not read `OTEL_EXPORTER_OTLP_TIMEOUT`, `OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_BSP_*` or `OTEL_EXPORTER_OTLP_PROTOCOL`, so it never checks them.

When `require_issued_identity=True`, `init_service` ignores `ENVIRONMENT`, `ENV`, and `APP_ENV`
and derives the tier from `deployment.environment.name`. When an OTLP endpoint is active, it
requires `OTEL_SERVICE_NAME` and the four `REQUIRED_IDENTITY_ATTRIBUTES`
(`deployment.environment.name`, `infra.service.id`, `service.version`, `infra.iac.ref`).

One strict failure is different: a failure while the providers start, for example when the exporter package is not installed. It happens after the probe routes exist. A retry on the same app raises `RuntimeError: init_service was already called for this app`.

With `strict_otel=False`, a telemetry problem never stops the service. A missing tier counts as `local_dev`. A problem is reported in one of two ways:

- A Python `RuntimeWarning` reports a malformed `OTEL_RESOURCE_ATTRIBUTES` (the SDK discards the attributes), an invalid `OTEL_SDK_DISABLED` (it counts as false) and an invalid `OTEL_TRACES_SAMPLER` or `OTEL_TRACES_SAMPLER_ARG` (the default sampler applies). The warning also appears when no endpoint is set.
- A log record of the logger `infra2_sdk.fastapi` reports every other problem. With an endpoint, the record is `Could not initialize OpenTelemetry: ...` at WARNING, and telemetry stays off. Without an endpoint, the only record is `OTEL_EXPORTER_OTLP_ENDPOINT is not set; OpenTelemetry export is off`. Its level is WARNING in a deployed tier and DEBUG elsewhere. An unknown or conflicting tier counts as deployed. The tier comes from the tier variables. When no tier variable is set and `OtelSettings.from_env` succeeds, it comes from `deployment.environment.name`. After a failed `OtelSettings.from_env`, only the tier variables count, so a problem in the settings can lower the level to DEBUG. With `OTEL_SDK_DISABLED=true` and no other problem, the record is `OpenTelemetry is disabled by the environment` at DEBUG, with or without an endpoint. An explicit off switch is not a misconfiguration.

Before OpenTelemetry attaches its logging handler, `init_service` calls `logging.basicConfig(level=log_level)` (default `INFO`). The call changes nothing when the root logger already has a handler. Pass `log_level=None` to skip it. The SDK serves no `/metrics` endpoint.

## Microservice scaffolder

Generate standard microservice skeletons with a single command:

```bash
# Generate pure 12-factor standalone service (.env, compose without Vault)
python -m infra2_sdk.scaffold my-service --profile standalone --db postgres --port 8000

# Or generate Dokploy/Vault sidecar service for the infra2 cluster
python -m infra2_sdk.scaffold my-service --profile infra2 --project apps --db postgres --port 8000
```

Generated assets include:
- `compose.yaml`: Hardened Docker Compose declaration with explicit memory ceilings and health check intervals.
- `app.py`: FastAPI application with `init_service` and database probe wiring.
- `entrypoint.sh`: Container bootstrap script applying migrations safely.
- `requirements.txt`: Curated dependencies with exact runtime extras (`infra2-sdk[fastapi,otel,postgres]`). The `infra2-sdk` lower bound is the `major.minor.patch` release of the SDK that generated the file.
- `Dockerfile`: Minimal container image recipe.
- `.env.example` (in standalone profile) or `deploy.py` and the Vault assets (in infra2 profile): `secrets.ctmpl` and `vault-policy.hcl` are rendered by `infra2_sdk.secrets` from one manifest, `vault-agent.hcl` configures the sidecar.

The generated `app.py` of a project with a database reads `DATABASE_URL`. It stops at import with `RuntimeError: DATABASE_URL is required` when the variable is unset, empty or blank. The generated compose file passes an empty string when the shell sets none.

## Environment contract

`runtime_env_contract()` is the machine-readable source for canonical names, compatibility
aliases, and sensitivity. Canonical names prefer existing open ecosystem conventions. The table
below is generated from it. Do not edit it by hand. After a change of the contract, replace the
text between the two markers with the output of `python scripts/render_env_contract.py`. Run
it in a source checkout: the wheel does not ship the script. The test suite fails when the
block is stale.

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
| `OTEL_EXPORTER_OTLP_ENDPOINT` | none | no |
| `OTEL_RESOURCE_ATTRIBUTES` | none | no |
<!-- env-contract:end -->

Canonical and alias values may coexist only when equal. A conflict between a canonical name and
an alias fails in `resolve_runtime_env` with `EnvironmentConflictError`. No variable of the
contract is sensitive today. The `sensitive` field stays for the wire shape, and
`resolve_env(..., sensitive=True)` keeps the values out of its error message. Missing OTLP
configuration disables telemetry without affecting application startup.
`OTEL_SDK_DISABLED=true` takes precedence over the endpoint and ignores it completely.

The table lists the variables that `environment_from_env` and `OtelSettings.from_env` resolve
through `RuntimeEnvKey`. The settings of the S3, PostgreSQL and HTTP adapters take explicit values,
so no row of the table belongs to them. Ten more fixed names exist outside the table.
`environment_from_env` reads `GITHUB_ACTIONS`. `OtelSettings.from_env` reads `INSTANCE_ID`,
`OTEL_SDK_DISABLED`, `OTEL_METRIC_EXPORT_INTERVAL`, `OTEL_TRACES_SAMPLER` and
`OTEL_TRACES_SAMPLER_ARG`. `VaultKvBackend.from_environ` reads `VAULT_ADDR`, `VAULT_TOKEN`,
`VAULT_ROLE_ID` and `VAULT_SECRET_ID` from the mapping you pass. Other names come from the caller:
the manifest that you pass to `validate_env`, and the token variable that a CLI option names.

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
- While infra2, finance_report and truealpha are the only consumers, a minor release may remove a
  public name. It may also change the default of a keyword. The census must cover the production
  and test code of the three consumers. It must find no use of the name and no reliance on the
  old default. The CHANGELOG states the change. A change that a consumer relies on needs a major
  release.
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
  - `infra2_sdk.dispatch` and `infra2_sdk.deploy_health`: use `infra2_sdk.deploy`;
  - `infra2_sdk.release`: use `infra2_sdk.refs`;
  - `infra2_sdk.runtime.environ`: use `infra2_sdk.runtime.environment`;
  - `infra2_sdk.runtime.dependencies` and `infra2_sdk.runtime.probes`: use
    `infra2_sdk.runtime.health`.
- In v3.3.0, `routing.LEGACY_CANARY_PR` and its `999` and `pr-999` slot mapping were removed.
  Those slot ids now resolve as ordinary preview slots. For the canary host, pass `tier="canary"`,
  `is_canary=True`, or `slot_id=CANARY_SLOT`.
- In v3.4.0, the SDK removed public names that no consumer uses. It follows the rule above: a
  census of infra2, finance_report and truealpha found no use of them. (v3.3.0 removed names
  that the README had marked as compatibility shims.) Replacements are in brackets.
  - `capacity.LEVELS`, `capacity.onepassword_capacity`, `capacity.OnePasswordCapacityReport` and
    `capacity.Runner`;
  - `delivery.PREVIEW_RELEVANT_STAGES`;
  - `deploy.DeployStatus` (no replacement: build the record you need in your own code);
  - `refs.resolve_release_identity` (use `refs.resolve_image_ref` and
    `refs.resolve_image_digest`);
  - `refs.verify_runtime_identity` (no replacement: compare the two records in your own code);
  - `refs.ReleaseIdentity` (no replacement: build the record you need in your own code);
  - `routing.resolve_dokploy_domains` and `routing.DokployDomainSpec`;
  - `runtime.config_schema.settings_json_schema`;
  - `runtime.environment.env_float` and `runtime.environment.env_bool`;
  - `runtime.health.assert_required_dependencies` and `runtime.health.DependencyUnavailableError`
    (use `check_health`);
  - `runtime.http.retryable_request` and `runtime.http.parse_retry_after` (no replacement: use
    httpx and your own `Retry-After` handling);
  - `runtime.http.HttpCheck` (use `probe_http`. It is only a reachability probe and does not
    validate the URL. `HttpCheck` raised `ValueError` for a bad URL);
  - `runtime.identity.runtime_identity_fingerprint`;
  - `runtime.otel.inject_trace_context` and `runtime.otel.extract_trace_context` (use
    `opentelemetry.propagate.inject` and `extract`. `extract` reads a plain dict
    case-sensitively, so pass lower-case keys or a case-insensitive mapping such as the Starlette
    `request.headers`. The removed helper lowercased the keys);
  - `runtime.postgres.PostgresCheck` and `runtime.s3.S3Check` (wrap `probe_postgres` or
    `probe_s3` in your own `DependencyCheck`):

    ```python
    from infra2_sdk.runtime import probe_postgres, probe_s3


    class DatabaseCheck:
        name = "database"

        def probe(self):
            return probe_postgres(db_settings)


    class ObjectStorageCheck:
        name = "object_storage"

        def probe(self):
            return probe_s3(storage_settings)
    ```

    The probes return the names `database` and `object_storage`. `run_probes` reports a check
    with another name as `absent`, with the detail `probe result name does not match check name`;
  - `secrets.EnvBackend` and `secrets.ReadOnlyBackendError`;
  - the module `infra2_sdk.snapshot`, with `AnonymizedSnapshotManifest`, `ResidualScanProof`,
    `ResidualScanStatus`, `SNAPSHOT_MANIFEST_VERSION`, `SnapshotArtifact`,
    `SnapshotArtifactFormat`, `SnapshotProducer` and `verify_snapshot_artifact` (no replacement:
    infra2 removed the last user, `tools/sync_anonymized_staging.sh`, on 2026-10-12. A project
    that still needs the manifest shape pins `infra2-sdk<3.4`).
- In v3.4.0, `S3Settings.from_env`, `HttpClientSettings.from_env` and `PostgresSettings.from_env`
  were removed. Build the settings with explicit values. The SDK no longer reads the variables
  below, and they left `RuntimeEnvKey` and `runtime_env_contract()`. boto3 may still read its
  own variables, such as `AWS_ACCESS_KEY_ID`, `AWS_DEFAULT_REGION` and `AWS_ENDPOINT_URL_S3`,
  when an S3 setting stays unset:
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
- In v3.4.0, `runtime_env_contract()["contract_version"]` (`RUNTIME_ENV_CONTRACT_VERSION`) is 2,
  not 1, because the document lost the 21 variables removed in 3.4.0 and 3.8.2. Version 2 lists 9
  variables. The field layout is the same. A receiver that requires version 1 rejects version 2, as
  the rule above says, so change it to require version 2.
- In v3.4.0, the SDK no longer reads `SERVICE_NAME`, the unused alias of `OTEL_SERVICE_NAME`.
  `ENV`, `APP_ENV` and `IMAGE_TAG` stay. A deployment that sets only `SERVICE_NAME` must set
  `OTEL_SERVICE_NAME` instead. Until then:
  - `OtelSettings.from_env` gives the service name `unknown_service` and raises no error;
  - when both variables are set, the SDK uses `OTEL_SERVICE_NAME` and reports no conflict.
    Before 3.4.0, two different values raised `EnvironmentConflictError`.
- In v3.4.0, a string lookup of a removed variable raises `ValueError`:
  `resolve_runtime_env(env, "DATABASE_URL")`, `runtime_env_spec("S3_BUCKET")` and
  `RuntimeEnvKey("AWS_REGION")`. Before 3.4.0, each of them returned a value.
- In v3.4.0, `init_service` is strict by default (`strict_otel=True`). Before, the default was
  `False`. A service without a known tier, or with an invalid `OTEL_*` value that the SDK reads,
  now stops at start-up. A caller that must never stop on telemetry passes `strict_otel=False`.
  The `init_service` documentation lists every strict check.
- In v3.4.0, the service name of the telemetry is `OTEL_SERVICE_NAME`, then `service.name` in
  `OTEL_RESOURCE_ATTRIBUTES`, then the `name` argument of `init_service`. Before, `init_service`
  used its `name` argument over the attribute. A padded value is trimmed. A value that is blank
  after decoding counts as missing.
- In v3.4.0, `init_service` raises `ValueError` for a blank `name` before it changes the app. A
  strict check that fails leaves no marker and no probe route. A failure while the providers
  start is the one exception. The `init_service` documentation describes it.
- In v3.4.0, 10 names left `__all__` of `infra2_sdk` and `infra2_sdk.runtime` and are no longer
  attributes of these packages (22 other names that left `__all__` in v3.4.0 or v3.5.1 were
  deleted or made private in v3.5.1, v3.5.2, v3.8.2, and v3.15.0 and are listed in the notes below).
  No consumer imports them from the package, and no recorded decision holds them. All 10 names below
  stay defined in their modules. Import them from the module, for example
  `from infra2_sdk.deploy import ProductionEvidencePolicy`:
  - in `infra2_sdk`:
    - `ci`: `validate_gate`;
    - `delivery`: `BudgetStatus`, `PipelineStage`;
    - `deploy`: `CONTRACT_VERSION`, `DeployEvidence`, `ProductionEvidencePolicy`;
  - in `infra2_sdk.runtime`:
    - `config_schema`: `validate_environment`;
    - `environment`: `EnvironmentConflictError`, `RuntimeEnvSpec`, `strict_environment_from_env`.
- In v3.4.0, `infra2_sdk.__version__` left `infra2_sdk.__all__` and stays importable. The root
  package no longer exposes `version` and `PackageNotFoundError`, which it imported from
  `importlib.metadata`.
- In v3.5.1, the SDK removed 12 zero-caller public symbols across `infra2`, `finance_report`, and
  `truealpha`: `cli.scaffold.main` (and the `infra2_sdk.cli` package), `deploy.build_deploy_request`,
  `deploy.derive_release_evidence`, `deploy.main`, `deploy.dispatch_main`,
  `deploy.deploy_health_main`, `routing.RouteEndpoint`, `routing.AppRoutePreference`,
  `runtime.environment.to_deploy_type`, `runtime.environment.APP_OWNED_TIERS`,
  `runtime.health.health_response`, and `runtime.otel.OtelSettings.from_identity`.
- In v3.5.2, the SDK added `PRODUCTION_REQUEST_MAX_AGE_SECONDS = 1440` in `infra2_sdk.deploy` (and
  `infra2_sdk.__all__`) and removed the remaining 3 zero-caller public symbols (`deploy.DeployState`,
  `delivery.acceleration_allowed`, and `delivery.detect_disagreement`).
- In v3.8.2, the SDK retired `infra2_sdk.runtime.identity` (`RuntimeIdentity` and
  `canonical_sha256`), `delivery.validate_stage_result`, and `RuntimeEnvKey.INSTANCE_ID` after all
  consumers converged on `OtelSettings(require_issued_identity=True)`. `INSTANCE_ID` remains a fixed
  environment read for `OtelSettings.from_env`.
- In v3.14.0, the SDK removed 17 zero-caller top-level symbols across `transport`
  (`TransportErrorKind`, `TransportFailure`, `classify_transport_error`, `ThrottlePolicy`,
  `paced_transport`, `decode_decimal_json`), `delivery` (`SmokeCheckResult`, `SmokeCheck`,
  `CanaryReport`, `SchemaDiscrepancy`, `compare_enum_definitions`,
  `validate_schema_compatibility`), and `deploy` (`FreshnessFailure`, `Staleness`,
  `measure_freshness`, `format_freshness_failure`, `check_deploy_freshness`), plus 16 zero-caller
  methods on `S3Settings`, `PostgresSettings`, `LlmSettings`, `EnvironmentManifest`,
  `DependencyManifest`, `ProbeResult`, and `VaultKvBackend`.
- `tests/surface.json` records the reason for each name in `__all__`: a consumer imports it, the
  README documents it, or a recorded decision holds it.
- `to_environment_tier` maps `DeployType` values to `EnvironmentTier`.
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
