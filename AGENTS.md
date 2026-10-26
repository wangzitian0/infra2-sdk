<!-- WS_STATIC_START adapter=rules-v2 inputs=a4f9ac50626ee8f9a34c96f9abb5871e29241d19e8d85fe54287161bc9406528 -->
<!-- Generated file: do not edit by hand. These rules are maintained in the owner's rule source and re-rendered here. -->

## Engineering discipline

- **Measure the physical system first.** Inspect the system with read-only probes (`time cmd`, process chains, file locks) before proposing architecture. Never combine validation and mutation in one command. Measure, read the result, then decide.
- **Green does not prove truth.** The tested system writes its own unit tests, CI, and issue states. Cross-check critical conclusions against two external sources not written by this repository.
- **Tests must be falsifiable.** Never hide assertions inside `if` branches or accept tautologies. Source-text `indexOf` matches against prose are not integration tests. Avoid duplicate test function names that shadow earlier tests. Make every new test fail under a relevant mutation; fake tests are CRITICAL merge blockers.
- **Falsifiable documentation and assertions:** State test requirements, error messages, and specifications in concrete ASD-STE100 terms without vague qualifiers.
- **One source of truth:** Keep one authoritative definition per core fact. Repeated hardcoding and scattered configuration invite drift.
- **Clean up during migration:** After the new mechanism is live and equivalence is proved, remove its predecessor, obsolete files, and dead code in the same change. Define contracts first and derive CI from them.
- **Deletion can leave guards green and empty:** A guard for an old structure can stop checking anything after deletion. Check each guard and remove it or redirect it to the new structure; green tests alone do not prove safe deletion.
- **Define guard scope from what it must govern, not from today's passing tree.** Let the guard fail on existing violations, then repair them. A guard never seen failing is not yet evidence of protection.
- **Worktree self-sufficiency:** Every worktree must resolve its dependencies, toolchain, skills, and configuration internally. Tools and tests must not navigate upward with `../..` to locate files in parent checkouts.
- **Physical context verification:** Static file existence does not prove host runtime context injection. Junior runs non-interactive probes (`-p` or headless exec) with low-cost models, asserting visible specification text across root and deep subdirectories.
- **Host verification scope:** Official host CLIs are the primary execution environment. Regular testing covers Antigravity 2.0 project workspaces (`.agents/skills/` and project rules); standalone desktop chat apps without directory awareness are excluded.
- **Local and remote test segregation:** Inner-loop verification (Senior, Junior, Intern) runs only focused tests on changed files (`pytest <file>::<test> -x`). Full-suite regression runs on remote CI, or locally at most once before merge.
- **Bounded file reads:** Pass `StartLine` and `EndLine` (`<= 250` lines per call) when reading existing files with `read_file` or `view_file`.

## Delivery and merge

- **ASD-STE100 specification for all artifacts:** Write committed artifacts, rules, code, comments, commits, and docs in ASD-STE100 English. Keep instructions `<= 20` words and descriptions `<= 25` words in active voice without vague qualifiers. Operational skills (`skills/**/SKILL.md`) follow the target repository's language policy.
- **Deliverable format:** Deliver a mergeable PR or a traceable issue, rather than a process-only report. Use issues and PRs as the collaboration bus. Search for an existing similar issue before creating one; update it if it exists.
- **Fail fast left to right:** Put the cheapest and likeliest failure checks first.
- **Review standing authorization:** Resolve a review thread after verifying in source code (not redacted diffs) that it is fixed or obsolete. Never resolve actionable or unverified feedback. Turn false reports into falsifiable invariant tests.
- **Weighted review gates:** Each repository defines its own severity weights and blocking thresholds. Read literal `severity: <level>` tags; do not infer severity from prose.
- **Merge when ready:** Once all merge conditions pass, merge and continue from the latest main rather than piling up divergent branches.
- **Delivery state invariant:** Never declare complete, done, or finished before merge to main and a verified Business Reality Probe (`ws-delivery-status`). State labels are `'Draft'`, `'Local verified'`, `'In review'`, `'Stage 1 Landed'`, and `'VERIFIED_COMPLETE'`.
- **Anti-exclusion and reality invariant:** Exclusion is not resolution. Never exclude failing entities or add exemptions to manufacture green checks. A task is complete only when a physical reality probe confirms non-empty execution.
- **Three-stage deployment prerequisite:** Stage 1 (Merge to main) is a non-bypassable physical prerequisite for Stage 2 and Stage 3. If HEAD is not on main, fail closed and refuse any deployment request.
- **Deploy provenance invariant:** Before dispatching any deploy workflow, physically verify that the target ref or tag contains current branch commits (`git merge-base --is-ancestor HEAD <target>`). Never deploy an older tag as completion evidence for current work.

## Production reservation

The single definition of the production reservation. Other tiers, skills and gates point here.

- **Only a production change returns to the owner.** A production change alters what production runs or stores. Examples: a deploy, promote or apply to production; a production secret write; a manual production data write or deletion. Also a production DNS, route or host change, and a release that cannot be recalled. Code that reaches production with no later owner approval, for example on a schedule, counts too. Each Repo tier names its production paths.
- **Approval:** the owner approves the exact commit SHA or release tag of each production change, and is present when it runs. A "deploy" reply covers only the release that it names. Immediately before a production dispatch, get a live reply from the owner.
- **Self-guard:** an edit that removes, narrows or redefines the production reservation needs the owner's approval of its head SHA. This includes an edit that makes fewer production runs wait for the owner. Examples: the owner-approval check of a deploy path, a production job classification, the settings of a production environment. It also includes an edit that removes or weakens a pin of this section or its dev_env pin test. The same holds for the required check that runs the test.
- **Everything else is the agent's:** in a repository that the owner owns, every merge, staging and canary deploy, and release tag. Also rule text, skills, gate code, data and tests, required-check workflows and release code. Before you merge gate or release code, a read-only reviewer that did not write it checks the head. The review states whether the gate gets stricter, stays equal or gets looser. A looser edit in the self-guard list follows the self-guard.
- **Every other hold is a defect:** a rule, skill, label or gate exit code that holds non-production work for the owner. Do the work, then fix the hold. The dev_env check `ws_owner_scope` fails on it.

# Infra2 SDK AI Agent Rules

> **Authority boundary:** An AI agent changes this file through its dev_env source and the dev_env rule-text checklist. Standing merge authority applies when every CI check and review gate passes. **Production reservation:** this repository has no production path (owner instruction, 2026-10-26: an SDK release tag runs with no environment and no approval). A release tag publishes a wheel and changes nothing that production runs. A consumer pins the wheel by URL and sha256 in its lock file. SDK code, gate code included, reaches production only through a consumer pin bump, which that consumer's merge gate and production path review.

## SDK role and contract boundaries

The `infra2-sdk` repository owns the public code contract between `infra2` and downstream applications (`finance_report`, `truealpha`).

1. **Contract and open-protocol adapter boundary (P1):** Own pure data contracts, offline validators, and explicitly invoked open-protocol adapters (`boto3`, `psycopg`, `httpx`, `opentelemetry`). Never add import-time I/O, network calls, or global provider configuration (`tests/test_exports.py`). Applications must run and pass CI with plain environment variables and zero `infra2` runtime dependency.
2. **One source per fact and Manifest v2 supply chain (P2):** Every environment field declares one `source` class (`bootstrap`, `human`, `runtime`, `release`, `decision`, `code`). Cross-repository generic libraries (`capacity`, `ci`, `delivery`, `deploy`, `fastapi`, `manifests`, `observability`, `refs`, `routing`, `rules`, `runtime`, `scaffold`, `secrets`, `transport`) live here so applications do not duplicate infrastructure logic. `LlmSettings` in `src/infra2_sdk/runtime/llm.py` owns default `large`, `small`, and `multimodal` model chains.
3. **100% consumer-reachable public surface (P3):** Every public symbol must have at least one production caller across `infra2`, `finance_report`, or `truealpha`, or an explicit standalone entry in `tests/surface.json`. Package public attributes must equal `__all__` (`tests/test_exports.py`). Prefix internal helpers with `_`.
4. **Locked environment reads and bidirectional guards (P4):** `tests/test_source_environment_reads.py` locks every environment variable read in `src/infra2_sdk/` to `RuntimeEnvKey` plus `FIXED_READS`. Do not read legacy aliases (`ENV`, `APP_ENV`, `IMAGE_TAG`, `SERVICE_NAME`) directly at call sites; resolve aliases only in `src/infra2_sdk/runtime/environment.py`. Keep `README.md` examples and module tables executable under `tests/test_readme_claims.py` and `tests/test_release_hygiene.py`.
5. **Pragmatic compatibility and release hygiene (P5):** Add new runtime dataclass fields as `kw_only=True`. Do not add compatibility shims for symbols with zero callers in the cross-repository usage census (`sdk_usage_census`). A change under `src/infra2_sdk/` does not need a release. A release PR updates `pyproject.toml`, `CHANGELOG.md`, and the `README.md` install pins together (`tests/test_release_hygiene.py`).

## Merge gate and release lifecycle

- Weight unresolved review findings by literal `severity: <level>` label: high=1.0, middle=0.5, low=0.25, unlabeled=middle. A total of 1.0 or more blocks merge.
- Required CI jobs in `.github/workflows/ci.yml` (`test` across Python 3.11–3.13, `package`, and `extras-smoke` across `core`, `s3`, `postgres`, `http`, `otel`, `fastapi`, `all`) must pass on the current `head SHA`.
- Merging to `main` is Stage 1 (Code Landed). The agent pushes a `vX.Y.Z` tag when a consumer needs the change. The tag triggers `.github/workflows/release.yml`, which publishes the wheel with no approval.

## Load on demand

| Task | Read |
|---|---|
| Read architectural charter and evolution principles (P1–P5) | [`init.md`](init.md) |
| Check module table, runtime extras, and usage examples | [`README.md`](README.md) |
| Follow development, census, and release procedures | [`CONTRIBUTING.md`](CONTRIBUTING.md) |
| Verify public surface and environment-read locks | `tests/surface.json`, `tests/test_exports.py`, `tests/test_source_environment_reads.py` |
<!-- WS_STATIC_END -->
