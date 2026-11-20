# Canary Todo

The Canary Todo service proves each SDK release on the real platform before an app adopts it.

- Code: `app.py`, `ui.py`. Required environment: `env.manifest.json`.
- Image: `Dockerfile` installs the SDK from this repository, so image `vX.Y.Z` runs SDK `X.Y.Z`. `release.yml` publishes `ghcr.io/wangzitian0/infra2-canary-todo:vX.Y.Z`.
- Tests: `pytest canary/todo/tests`.
- Deploy and probes: infra2 `platform/30.todo` and `docs/ssot/platform.canary_todo.md`.
- The wheel does not contain this directory.
