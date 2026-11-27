# Contributing

The [README](README.md) defines the public 6-module contract; [`pyproject.toml`](pyproject.toml) defines the version and core dependency surface (`pydantic`, `pyyaml`).

## Ownership

- Keep public modules flat (`config`, `telemetry`, `service`, `storage`, `llm`, `delivery`) with `_core.py` as the sole internal helper module.
- Imports must stay lazy and perform zero filesystem/network I/O or global provider mutation.
- Maintain 1:1 positive tests per module (`tests/test_<module>.py`) and `tests/test_architecture_guards.py` without legacy shims or tombstone tests.

## Local Pre-Flight & Releasing

- Before opening a PR, run focused formatting, linting, and unit tests for changed modules (full matrix + container stack verification run in CI on PR push).
- To release after PR merge and green `main` CI: bump `version` in `pyproject.toml`, update [CHANGELOG.md](CHANGELOG.md), [README.md](README.md), [`docs/app-integration-contract.md`](docs/app-integration-contract.md), and [`tests/test_architecture_guards.py`](tests/test_architecture_guards.py), run `uv lock`, merge to `main`, wait for post-merge `ci.yml` to pass on the merge commit SHA, then push tag `vX.Y.Z`. `.github/workflows/release.yml` publishes immutable GitHub Release artifacts and `ghcr.io/wangzitian0/infra2-canary-todo:vX.Y.Z`.
