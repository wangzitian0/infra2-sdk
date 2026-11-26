# infra2-sdk Project Charter (init.md)

- **Runtime boundary**: Applications read canonical environment variables (`ENVIRONMENT`, `DATABASE_URL`, `REDIS_URL`, `S3_*`, `LLM_*`, `OTEL_*`) directly via `infra2_sdk.config`. No runtime Vault/control-plane coupling belongs in `infra2-sdk`.
- **Control-plane separation**: Deployment compilation, Traefik routing, Vault templating, and infrastructure capacity orchestration live in `infra2`.
- **Zero-I/O on import & minimal core (`pyproject.toml`)**: Core depends only on `pydantic>=2.7,<3` and `pyyaml>=6.0,<7`. All six public modules (`config`, `telemetry`, `service`, `storage`, `llm`, `delivery`) share a single internal foundation (`_core.py`) and load optional adapters lazily.
- **One authoritative contract base**: Shared contracts inherit `infra2_sdk._core.ContractModel` (`frozen=True`, `extra="forbid"`, `populate_by_name=True`).
- **End-to-end Canary proof**: `canary/todo` exercises all six modules against real PostgreSQL, Redis, RustFS S3, OTLP HTTP, worker probes, schema gate, delivery hooks, and VCR LLM cassettes.
