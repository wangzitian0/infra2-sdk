# Contributing

The [README](README.md) defines this package's ownership and public compatibility
contract; `pyproject.toml` defines the release version and dependency surface.

## Ownership

- Share stable data contracts and explicitly invoked protocol adapters when consumers
  need the same semantics. Imports must not perform I/O or configure global providers.
- Applications own domain policy, dependency requirements and settings models. Infra2
  owns deployment orchestration and when credentials or infrastructure are mutated.
- OMCA owns coding-agent observation, profiles and isolated runtime generations.
  Agent workflow policy and repository management do not belong in this runtime SDK.
- Consumers install an exact released artifact; never depend on a sibling checkout.

## Changes and proof

Use an independent branch and PR. Preserve public import paths and wire compatibility;
apply the README's semantic-version rules before release. Add regression tests for
changed behavior, including failure paths and secret redaction where applicable.

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest --cov
uv run python -m build
```

Run focused tests during development. Release changes require the repository CI and
wheel checks in `.github/workflows/`. Report the revision tested and any missing proof;
do not treat a locally built wheel as a published release or update consumers to it.
