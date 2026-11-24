# Canary Todo

The Canary Todo service proves each SDK release across API, worker, schema, and capability paths before applications adopt it.

- Code: `app.py`, `ui.py`, `worker.py` (`init_worker` + `request_with_retry`), `canary_settings.py`, `canary_schema.py` (`schema_check` metadata contract).
- Environment: `canary_settings.py` is the single reader of the process environment and generates `env.manifest.json` and `env-reference.generated.md`:
  `cd canary/todo && python -m infra2_sdk.runtime.config_schema canary_settings:CanarySettings --source canary/todo --json env.manifest.json --markdown env-reference.generated.md`.
  No default names a host; optional capability variables (`OPENPANEL_HEALTH_URL`, `OPENPANEL_*`, `NEXT_PUBLIC_*`) default empty.
- Hooks: `runtime-hooks.manifest.json` declares the Schema-3 `smoke` hook invoking `worker.py --once`.
- Without a platform: `docker compose -f canary/todo/compose.yaml up --build` runs Postgres (with `db-init` enum setup), Redis, S3, and the web service. `scripts/check_canary_standalone.sh` verifies `/api/canary/status`, `worker.py --once`, and `infra2_sdk.runtime.schema_check`.
- Image: `Dockerfile` installs the SDK from this repository, so image `vX.Y.Z` runs SDK `X.Y.Z`. `release.yml` publishes `ghcr.io/wangzitian0/infra2-canary-todo:vX.Y.Z`.
- Tests: `pytest canary/todo/tests`.
- Deploy and probes: infra2 `platform/30.todo` and `docs/ssot/platform.canary_todo.md`.
- The wheel does not contain this directory.
