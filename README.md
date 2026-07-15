# infra2-sdk

Versioned, side-effect-free contracts shared by
[`infra2`](https://github.com/wangzitian0/infra2),
[`finance_report`](https://github.com/wangzitian0/finance_report), and
[`truealpha`](https://github.com/wangzitian0/truealpha).

The SDK is the dependency boundary between applications and infrastructure. It owns data
models, validation, and serialization. It does **not** own infrastructure execution:
Dokploy/Vault clients, compose files, service discovery, deployment mutation, backups, and
host operations remain in `infra2`.

## Install

Until the first tag is published, install from a pinned Git commit:

```bash
python -m pip install \
  "infra2-sdk @ git+https://github.com/wangzitian0/infra2-sdk.git@<commit>"
```

After release, consumers should pin a tag and update deliberately:

```bash
python -m pip install \
  "infra2-sdk @ git+https://github.com/wangzitian0/infra2-sdk.git@v0.1.0"
```

## Modules

| Module | Ownership |
|---|---|
| `infra2_sdk.delivery` | Environment/stage evidence and failure taxonomy |
| `infra2_sdk.ci` | Delivery-stage vocabulary and CI gate inventory validation |
| `infra2_sdk.deploy` | Versioned deploy request/status wire contract |
| `infra2_sdk.refs` | Pure Git ref classification and resolution |

## Compatibility

- Semantic versions describe the public Python and serialized JSON contracts.
- Additive fields and enum values require a minor release.
- Removing or changing an existing field requires a major release.
- Receivers must reject unsupported `contract_version` values before side effects.
- Repository submodules are development workspace pointers, not package dependencies.

## Development

```bash
python -m pip install -e '.[dev]'
ruff check .
pytest --cov
python -m build
```
