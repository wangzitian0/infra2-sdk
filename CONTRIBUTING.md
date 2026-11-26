# Contributing

The [README](README.md) defines the public 6-module contract; [`pyproject.toml`](pyproject.toml) defines the `4.10.2` version and minimal core dependency surface (`pydantic`, `pyyaml`).

## Ownership

- Share stable Pydantic `ContractModel` schemas and explicit open-protocol adapters (`config`, `telemetry`, `service`, `storage`, `llm`, `delivery`). Imports must stay lazy and perform zero I/O or global provider mutation.
- Applications own domain logic, dependency manifests, SQL models, and settings classes (`CanarySettings`). Infrastructure orchestration stays in `infra2`.
- Keep modules strictly MECE: zero cross-module private imports, zero legacy fallback shims inside the current SDK, and 1:1 positive tests per module.

## Changes and Verification

Before opening or merging a PR:

1. Run `.venv/bin/ruff format --check . && .venv/bin/ruff check .`.
2. Run `.venv/bin/pytest --cov --cov-report=term-missing` (including Podman PostgreSQL catalog tests when available) and `.venv/bin/pytest canary/todo/tests -q`.
3. Verify `python -m build` and `tests/wheel_smoke.py` across all 8 extras (`core`, `telemetry`, `http`, `fastapi`, `postgres`, `s3`, `llm`, `all`).
4. Run `scripts/check_canary_image_sdk.sh` and `scripts/check_canary_standalone.sh`.

## Releasing

1. Update [CHANGELOG.md](CHANGELOG.md), bump `version` in `pyproject.toml`, run `uv lock`, and open/merge the PR to `main`.
2. After `ci.yml` on `main` (`push`) passes on the merge commit SHA, tag `vX.Y.Z` on `main` and push the tag.
3. Let `.github/workflows/release.yml` build the distribution, create the GitHub Release, and publish `ghcr.io/wangzitian0/infra2-canary-todo:vX.Y.Z` without manual release creation.
