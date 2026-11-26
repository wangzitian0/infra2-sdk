# Canary Todo

The Canary Todo service proves all six `infra2_sdk` modules end-to-end across API, worker, schema, and delivery hook paths before downstream applications adopt a release.

- Code: `app.py`, `ui.py`, `worker.py`, `canary_settings.py`, `canary_schema.py`.
- Environment: `canary_settings.py` (`CanarySettings`) is the single process environment reader and generates `EnvironmentManifest` directly at startup without checked-in duplicate JSON/Markdown artifacts.
- Hooks: `runtime-hooks.manifest.json` declares the Schema v3 `smoke` hook invoking `worker.py --once`.
- Standalone verification: `scripts/check_canary_standalone.sh` brings up `canary/todo/compose.yaml` (PostgreSQL, Redis, RustFS S3, OTLP stub, web service) and exercises `/api/canary/status`, `/livez`, `/readyz`, `/api/health`, Todo CRUD, `worker.py --once --validate-env`, `infra2_sdk.storage check-schema`, fault injection (`503 -> 200`), and `infra2_sdk.delivery check-hooks` / `run-cell`.
- Image: `Dockerfile` installs `infra2-sdk` directly from this repository (`ghcr.io/wangzitian0/infra2-canary-todo:vX.Y.Z`).
- Tests: `pytest canary/todo/tests`.
