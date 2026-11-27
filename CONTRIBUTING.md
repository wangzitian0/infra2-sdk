# Contributing

[README.md](README.md) defines the 6-module public contract; [`pyproject.toml`](pyproject.toml) defines the package version and core dependencies (`pydantic`, `pyyaml`).

## Architecture Rules

- Keep public modules flat (`config`, `telemetry`, `service`, `storage`, `llm`, `delivery`) with `_core.py` as the sole internal helper module.
- Keep module imports lazy with zero filesystem, network, or global OTel provider mutation.
- Prefix all module-internal helpers and constants with `_`; `tests/test_architecture_guards.py` enforces `__all__` and `vars(mod)` parity.
- Maintain 1:1 test modules (`tests/test_<module>.py`) without legacy shims or tombstone tests.

## Local Pre-Flight & Releasing

- **Local pre-flight** (`local.pre` in `src/infra2_sdk/data/stages.yaml`): run lint, format check, and focused unit tests for changed modules in one process (`uv run --no-sync ruff check . && uv run --no-sync ruff format --check . && uv run --no-sync pytest tests/test_<mod>.py -x`). The full Python 3.11–3.13 matrix, `extras-smoke`, and `canary-todo` container stack run in CI on PR push.
- **Release**: after PR merge and green `main` CI, bump `version` in `pyproject.toml`, update [CHANGELOG.md](CHANGELOG.md), [README.md](README.md), [`docs/app-integration-contract.md`](docs/app-integration-contract.md), and [`tests/test_architecture_guards.py`](tests/test_architecture_guards.py), run `uv lock`, merge to `main`, wait for `ci.yml` to pass on that merge commit SHA, then push tag `vX.Y.Z`. `.github/workflows/release.yml` publishes immutable GitHub Release artifacts and `ghcr.io/wangzitian0/infra2-canary-todo:vX.Y.Z`.
