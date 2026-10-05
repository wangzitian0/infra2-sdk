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

## Releasing

A release changes the version in the same PR and is published by the workflow, never by hand.

1. In the PR: bump `version` in `pyproject.toml` (refresh `uv.lock` with `uv lock`), add the
   [CHANGELOG](CHANGELOG.md) entry, and update the version pins in the README.
   `tests/test_release_hygiene.py` fails when any of the three is stale.
2. After the PR is merged: tag the merge commit on `main` as `vX.Y.Z` and push only the tag.
   `release.yml` verifies the tag matches the package version, re-runs lint and tests, builds,
   smoke-tests every extra against the wheel, and creates the GitHub release from those
   artifacts (`gh release create --verify-tag`).
3. Do not create the release yourself, with `gh release create` or in the web UI, before the
   run finishes. The workflow's publish step then fails with "a release with the same tag name
   already exists" (as for v2.3.1) and the release carries assets the workflow did not build.
   If it happens, treat the release as unverified: do not overwrite its assets silently, and
   decide on the fix in [#18](https://github.com/wangzitian0/infra2-sdk/issues/18), which owns
   release identity.
