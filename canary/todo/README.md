# Canary Todo

The Canary Todo service proves each SDK release on the real platform before an app adopts it.

- Code: `app.py`, `ui.py`, `canary_settings.py`.
- Environment: `canary_settings.py` is the one reader of the process environment. It generates `env.manifest.json`:
  `cd canary/todo && python -m infra2_sdk.runtime.config_schema canary_settings:CanarySettings --source canary/todo --json env.manifest.json`.
  No default names a host. A check without its address is `unconfigured`.
- Without a platform: `docker compose -f canary/todo/compose.yaml up --build` starts the app with Postgres, Redis and an S3 store. `scripts/check_canary_standalone.sh` requires a green verdict from it.
- Image: `Dockerfile` installs the SDK from this repository, so image `vX.Y.Z` runs SDK `X.Y.Z`. `release.yml` publishes `ghcr.io/wangzitian0/infra2-canary-todo:vX.Y.Z`.
- Tests: `pytest canary/todo/tests`.
- Deploy and probes: infra2 `platform/30.todo` and `docs/ssot/platform.canary_todo.md`.
- The wheel does not contain this directory.
