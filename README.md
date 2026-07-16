# infra2-sdk

Versioned, side-effect-free runtime contracts shared by
[`infra2`](https://github.com/wangzitian0/infra2),
[`finance_report`](https://github.com/wangzitian0/finance_report), and
[`truealpha`](https://github.com/wangzitian0/truealpha).

The SDK is a long-lived application dependency, not an infra2 client. It owns a stable
environment-variable vocabulary, validation, serialization, and thin adapters over open
runtime protocols. A standalone process passes ordinary environment variables; infra2 may
derive the same variables from its multi-environment deployment coordinate. Both paths execute
the same local SDK code.

The SDK performs no infrastructure execution. Dokploy/Vault clients, compose files, service
discovery, deployment mutation, backups, and host operations remain outside this package. No
`INFRA2_*`, Vault, or Dokploy value is required to load runtime settings.

## Install

Consumers should pin a release and update deliberately:

```bash
python -m pip install \
  "infra2-sdk @ git+https://github.com/wangzitian0/infra2-sdk.git@v0.3.0"
```

## Modules

| Module | Ownership |
|---|---|
| `infra2_sdk.delivery` | Environment/stage evidence and failure taxonomy |
| `infra2_sdk.ci` | Delivery-stage vocabulary and CI gate inventory validation |
| `infra2_sdk.deploy` | Versioned deploy request/status wire contract |
| `infra2_sdk.refs` | Pure Git ref classification and resolution |
| `infra2_sdk.runtime.environment` | Canonical six-tier environment vocabulary and aliases |
| `infra2_sdk.runtime.environ` | Versioned canonical env registry and conflict-safe resolution |
| `infra2_sdk.runtime.config_schema` | JSON Schema 2020-12 and environment injection manifests |
| `infra2_sdk.runtime.dependencies` | Dependency declaration and per-tier requirements |
| `infra2_sdk.runtime.probes` | Sync/async probe contract, runner, and required-dependency gate |
| `infra2_sdk.runtime.s3` | Standard boto3 S3 client, probe, and safe primitives |
| `infra2_sdk.runtime.postgres` | PostgreSQL DSN normalization and psycopg probe |
| `infra2_sdk.runtime.http` | Standard httpx clients and HTTP retry semantics |
| `infra2_sdk.runtime.otel` | Explicit OTLP trace/metric/log provider bootstrap |
| `infra2_sdk.runtime.identity` | OCI/config/release identity and OTel resource coordinates |

## Runtime extras

The core runtime contracts have no runtime dependency beyond the SDK core. Install only the
open-protocol adapters an application uses:

```bash
python -m pip install \
  'infra2-sdk[s3,postgres,otel,http] @ git+https://github.com/wangzitian0/infra2-sdk.git@v0.3.0'
# or, for a conformance canary:
python -m pip install \
  'infra2-sdk[all] @ git+https://github.com/wangzitian0/infra2-sdk.git@v0.3.0'
```

Adapter modules deliberately return standard library objects rather than infra2-specific
storage, database, HTTP, or telemetry abstractions:

- S3 returns a boto3/botocore client. Object keys, immutability, checksums, lifecycle, and
  public access remain application policy.
- PostgreSQL accepts a PostgreSQL URI and performs only a `SELECT 1` reachability probe.
- HTTP returns an httpx client. Provider retry budgets and idempotency policy remain with the
  caller.
- OpenTelemetry configures OTLP/HTTP providers and W3C Trace Context propagation only when
  explicitly requested.

```python
from infra2_sdk.runtime import RuntimeIdentity, environment_from_env
from infra2_sdk.runtime.postgres import PostgresSettings
from infra2_sdk.runtime.s3 import S3Settings, create_s3_client

runtime = environment_from_env()       # reads os.environ only when called
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

## Environment contract

`runtime_env_contract()` is the machine-readable source for canonical names, compatibility
aliases, and sensitivity. Canonical names prefer existing open ecosystem conventions:

| Concern | Canonical names | Compatibility aliases |
|---|---|---|
| Runtime | `ENVIRONMENT`, `OTEL_SERVICE_NAME`, `SERVICE_VERSION`, `GIT_COMMIT_SHA`, `INSTANCE_ID` | `ENV`, `APP_ENV`, `SERVICE_NAME`, `IMAGE_TAG` |
| PostgreSQL | `DATABASE_URL`, `DATABASE_CONNECT_TIMEOUT_SECONDS` | — |
| S3 | `OBJECT_STORAGE_PROTOCOL=s3`, `S3_BUCKET`, `AWS_ENDPOINT_URL_S3`, `AWS_REGION`, standard AWS credentials | `OBJECT_STORAGE_DRIVER`, `S3_ENDPOINT`, `S3_REGION`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` |
| Telemetry | `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_SDK_DISABLED` | — |

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
- Receivers must reject unsupported `contract_version` values before side effects.
- Repository submodules are development workspace pointers, not package dependencies.
- Importing any runtime module performs no network I/O and mutates no global provider state.
- v0.2 ownership constants and `vault=True` manifest metadata remain compatibility-only; new
  consumers use tier semantics and explicit `injected=True` metadata.

## Development

```bash
python -m pip install -e '.[dev]'
ruff check .
pytest --cov
python -m build
```
