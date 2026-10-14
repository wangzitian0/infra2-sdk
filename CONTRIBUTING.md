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

The release workflow publishes each release. Change the version in the same PR. Do not publish manually.

0. A release that removes a public name or changes a default requires a usage census.
   Run `python -m tools.sdk_usage_census --json --check` in `infra2` across `infra2`,
   `finance_report`, and `truealpha`. The census checks `M1` symbol reachability,
   `M2` `runtime_env_contract()` reachability, `M3` pin lag, `M4` `LEFT_THE_PACKAGES`
   references, and `M5` hardcoded `SERVICE_NAME` lists. Attach the output to the release issue.
   Do not remove a name that a consumer uses in a minor release.
1. In the PR, bump `version` in `pyproject.toml` and run `uv lock`. Add the
   [CHANGELOG](CHANGELOG.md) entry and update the README version pins.
   `tests/test_release_hygiene.py` fails when any of the three files is stale. When a pull
   request modifies `src/infra2_sdk/`, `.github/workflows/ci.yml` requires a new `pyproject.toml`
   version and an unused `v<version>` git tag.
2. After you merge the PR, tag the merge commit on `main` as `vX.Y.Z` and push the tag.
   `.github/workflows/release.yml` runs under `environment: production` and requires reviewer
   approval. The workflow verifies that `$GITHUB_SHA` is on `origin/main`
   (`git merge-base --is-ancestor "$GITHUB_SHA" origin/main`). It also verifies that the tag
   matches `pyproject.toml`, runs lint and tests, smoke-tests each wheel extra, and runs
   `gh release create --verify-tag`.
3. Do not create the GitHub release manually before the workflow finishes. A manual release
   causes the publish step to fail and leaves unverified assets attached to the tag.
   Follow the release identity contract in `.github/workflows/ci.yml` and
   `.github/workflows/release.yml` ([#18](https://github.com/wangzitian0/infra2-sdk/issues/18)).
