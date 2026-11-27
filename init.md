# infra2-sdk Project Charter (`init.md`)

`infra2-sdk` owns the pure runtime contracts and explicit open-protocol adapters shared by `infra2`, downstream applications (`finance_report`, `truealpha`), and `canary/todo`.

## Architectural Boundaries (P1–P5)

1. **Contract and adapter boundary (P1)**:
   - `infra2_sdk.config` owns `EnvironmentTier`, `RuntimeEnvKey`, and offline/startup validation (`EnvironmentManifest`, `assert_env`) against `src/infra2_sdk/data/shared_env.yaml`.
   - Domain settings (`PostgresSettings`, `RedisSettings`, `S3Settings` in `storage`; `LlmSettings` in `llm`) read their canonical environment variables only when `.from_env()` is called.
   - `infra2_sdk` carries zero Vault, Traefik, Compose, or deployment control-plane logic; those stay in `infra2`.
2. **Zero-I/O lazy core (P2)**:
   - Core depends only on `pydantic>=2.7,<3` and `pyyaml>=6.0,<7`.
   - All six public modules (`config`, `telemetry`, `service`, `storage`, `llm`, `delivery`) share `infra2_sdk._core` and load optional drivers lazily with zero import-time filesystem, network, or global OTel mutation.
3. **One contract base (P3)**:
   - Shared configuration and manifest models inherit `infra2_sdk._core.ContractModel` (`frozen=True`, `extra="forbid"`, `populate_by_name=True`).
4. **100% consumer-reachable public surface (P4)**:
   - Every symbol in each module's `__all__` has a real caller across `infra2`, `finance_report`, `truealpha`, or `canary/todo`.
   - `tests/test_architecture_guards.py` enforces `__all__` parity, `vars(mod)` hygiene, OpenAPI schema naming, optional-extra alignment, and `README.md` table accuracy.
5. **End-to-end Canary proof (P5)**:
   - `canary/todo` exercises all six modules against real PostgreSQL, Redis, RustFS S3, OTLP HTTP, worker `SIGTERM` lifecycle, schema gate, delivery hooks, and VCR LLM cassettes (`scripts/check_canary_standalone.sh`).
