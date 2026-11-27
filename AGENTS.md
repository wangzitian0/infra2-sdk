# Infra2 SDK AI Agent Rules

> **Authority boundary:** This repository owns its own `AGENTS.md` directly. Standing merge authority applies when every CI check and review gate passes. **Production reservation:** this repository has no production path (owner instruction, 2026-10-26: an SDK release tag runs with no environment and no approval). A release tag publishes a wheel and changes nothing that production runs. A consumer pins the wheel by URL and sha256 in its lock file. SDK code, gate code included, reaches production only through a consumer pin bump, which that consumer's merge gate and production path review.

## Production reservation

The single definition of the production reservation. Other tiers, skills and gates point here.

- **Only a production change returns to the owner.** A production change alters what production runs or stores (deploy, promote or apply to production; production secret write; manual production data write or deletion; production DNS, route or host change; unrecallable release; or scheduled unapproved production code). Each Repo tier names its production paths. A path can be a merge or a release-checkout pull.
- **Approval:** the owner approves the exact commit SHA or release tag of each production change, and is present when it runs. A "deploy" reply covers only the release that it names. Immediately before a production dispatch, get a live reply from the owner.
- **Self-guard:** an edit that removes, narrows or redefines the production reservation needs the owner's approval of its head SHA. This includes an edit that makes fewer production runs wait for the owner (deploy-path approval check, production job classification, or production environment settings). It also covers removing or weakening a pin of this section, its pin test, or the required check that runs the test.
- **Everything else is the agent's:** in any repository the owner owns, every non-production change (including `dev_env` outside the self-guard), staging and canary deploy, release tag, rule text, skill, gate/release code, and required-check edit. Before merging gate or release code, an independent read-only reviewer inspects the head SHA and states whether the gate gets stricter, equal or looser. A looser edit in the self-guard follows the self-guard.
- **Every other hold is a defect:** a rule, skill, label or gate holding non-production work for the owner is a defect. Do the work, then fix the hold. `ws_owner_scope` fails on it.

## SDK role and contract boundaries

The `infra2-sdk` repository owns the public code contract between `infra2` and downstream applications (`finance_report`, `truealpha`).

1. **Contract and open-protocol adapter boundary (P1):** Own pure data contracts, offline validators, and explicitly invoked open-protocol adapters (`boto3`, `psycopg`, `httpx`, `opentelemetry`). Never add import-time I/O, network calls, or global provider configuration (`tests/test_architecture_guards.py`). Applications must run and pass CI with plain environment variables and zero `infra2` runtime dependency.
2. **One source per fact and Manifest v2 supply chain (P2):** Every environment field declares one `source` class (`bootstrap`, `human`, `runtime`, `release`, `decision`, `code`). Cross-repository generic libraries (`config`, `telemetry`, `service`, `storage`, `llm`, `delivery`) live here so applications do not duplicate infrastructure logic. `LlmSettings` in `src/infra2_sdk/llm.py` owns default `large_model`, `small_model`, and `multimodal_model` strings.
3. **100% consumer-reachable public surface (P3):** Every public symbol has a production caller across `infra2`, `finance_report`, or `truealpha`. Package public attributes match `__all__` under `tests/test_architecture_guards.py`. Prefix internal helpers with `_`.
4. **Locked environment reads and bidirectional guards (P4):** `tests/test_config.py` guards `RuntimeEnvKey` reads and retired aliases (`APP_ENV`, `IMAGE_TAG`, `SERVICE_NAME`); `tests/test_architecture_guards.py` guards package exports, lazy imports, and README parity. Keep `README.md` module tables accurate.
5. **Pragmatic compatibility and release hygiene (P5):** Runtime and schema contracts subclass `ContractModel` (`_core.py`); keep additive config/field changes backwards-compatible where consumers depend on them. Do not add compatibility shims for unused symbols. A change under `src/infra2_sdk/` does not need a release. A release PR updates `pyproject.toml`, `uv.lock`, `CHANGELOG.md`, `README.md`, `docs/app-integration-contract.md`, and `tests/test_architecture_guards.py` together (`CONTRIBUTING.md`).

## Merge gate and release lifecycle

- Weight unresolved review findings by literal `severity: <level>` label: high=1.0, middle=0.5, low=0.25, unlabeled=middle. A total of 1.0 or more blocks merge.
- Required CI jobs in `.github/workflows/ci.yml` (`test` across Python 3.11–3.13, `package`, `extras-smoke` across `core`, `telemetry`, `http`, `fastapi`, `postgres`, `s3`, `llm`, `all`, and `canary-todo`) must pass on the current `head SHA`.
- Merging to `main` is Stage 1 (Code Landed). The agent pushes a `vX.Y.Z` tag when a consumer needs the change. The tag triggers `.github/workflows/release.yml`, which publishes the wheel with no approval.

## Load on demand

| Task | Read |
|---|---|
| Read architectural charter and 6-module scope | [`init.md`](init.md) |
| Check installation extras and public module surface | [`README.md`](README.md) |
| Check application integration contract | [`docs/app-integration-contract.md`](docs/app-integration-contract.md) |
| Follow development and release procedures | [`CONTRIBUTING.md`](CONTRIBUTING.md) |
| Verify package layout and architecture guards | [`tests/test_architecture_guards.py`](tests/test_architecture_guards.py) |
