# infra2-sdk

Versioned, side-effect-free contracts shared by
[`infra2`](https://github.com/wangzitian0/infra2),
[`finance_report`](https://github.com/wangzitian0/finance_report), and
[`truealpha`](https://github.com/wangzitian0/truealpha).

The SDK is the dependency boundary between applications and infrastructure. It owns data
models, validation, serialization, and thin adapters over open runtime protocols. It does
**not** own infrastructure execution: Dokploy/Vault clients, compose files, service discovery,
deployment mutation, backups, and host operations remain in `infra2`.

## Install

Until the first tag is published, install from a pinned Git commit:

```bash
python -m pip install \
  "infra2-sdk @ git+https://github.com/wangzitian0/infra2-sdk.git@<commit>"
```

After release, consumers should pin a tag and update deliberately:

```bash
python -m pip install \
  "infra2-sdk @ git+https://github.com/wangzitian0/infra2-sdk.git@v0.2.0"
```

## Modules

| Module | Ownership |
|---|---|
| `infra2_sdk.delivery` | Environment/stage evidence and failure taxonomy |
| `infra2_sdk.ci` | Delivery-stage vocabulary and CI gate inventory validation |
| `infra2_sdk.deploy` | Versioned deploy request/status wire contract |
| `infra2_sdk.refs` | Pure Git ref classification and resolution |
| `infra2_sdk.runtime.environment` | Canonical six-tier environment vocabulary and aliases |
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
python -m pip install 'infra2-sdk[s3,postgres,otel,http]==0.2.0'
# or, for a conformance canary:
python -m pip install 'infra2-sdk[all]==0.2.0'
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
from infra2_sdk.runtime import EnvironmentTier, RuntimeIdentity
from infra2_sdk.runtime.s3 import S3Settings, create_s3_client

identity = RuntimeIdentity(
    service_name="example-api",
    service_version="1.2.3",
    environment=EnvironmentTier.STAGING,
    commit_sha="a" * 40,
    image_digest="sha256:" + "b" * 64,
    configuration_sha256="c" * 64,
    release_id="release-123",
)
identity.validate_protected()

s3 = create_s3_client(S3Settings(bucket="example-artifacts"))
```

## Compatibility

- Semantic versions describe the public Python and serialized JSON contracts.
- Additive fields and enum values require a minor release.
- Removing or changing an existing field requires a major release.
- Receivers must reject unsupported `contract_version` values before side effects.
- Repository submodules are development workspace pointers, not package dependencies.
- Importing any runtime module performs no network I/O and mutates no global provider state.

## Development

```bash
python -m pip install -e '.[dev]'
ruff check .
pytest --cov
python -m build
```
