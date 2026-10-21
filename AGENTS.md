<!-- WS_STATIC_START adapter=rules-v2 inputs=1f452bc71a96ce48db680bd361cdbf6c9a2577d1ba14791eda4eec92a3e331bb -->
<!-- Generated file: do not edit by hand. These rules are maintained in the owner's rule source and re-rendered here. -->

## Engineering discipline

- **Measure the physical system first.** Before an abstract architecture proposal, inspect the system with read-only probes such as `time cmd`, process chains, and file-descriptor locks. A conceptually neat story without physical evidence is insufficient. A probe must not write. Do not combine validation and action in one command: a POST permission probe can create a resource, and a trial commit can leave a real commit. Measure, read the result, then decide, with a stop point between these steps.
- **Green does not prove truth.** The tested system writes its own unit tests, CI, and issue states. Cross-check critical conclusions against two external sources not written by this repository.
- **Tests must be falsifiable.** Do not hide assertions in `if (exists)` or `if (code != 0)` so failures run zero assertions. Do not accept tautologies such as `typeof null === 'object'` or `result !== undefined || true`. Source-text `indexOf` matches against prose are not integration tests. Duplicate test function identifiers can silently shadow earlier tests (Python `def` and duplicate JS function/const/export names); duplicate string titles in `test()` or `it()` instead run both. A green count is not coverage evidence. Make a new test fail under a relevant mutation. A read-only reviewer treats such fake-test patterns as CRITICAL merge blockers.
- **Falsifiable documentation and assertions:** State test requirements, error messages, and specifications in concrete ASD-STE100 terms without vague qualifiers.
- **One source of truth:** Keep one authoritative definition per core fact. Repeated hardcoding and scattered configuration invite drift.
- **Clean up during migration:** After the new mechanism is live and equivalence is proved, remove its predecessor, obsolete files, and dead code in the same change. Define contracts first and derive CI from them.
- **Deletion can leave guards green and empty:** A guard for an old structure can stop checking anything after deletion. Check each guard and remove it or redirect it to the new structure; green tests alone do not prove safe deletion.
- **Define guard scope from what it must govern, not from today's passing tree.** Let the guard fail on existing violations, then repair them. A guard never seen failing is not yet evidence of protection.
- **Worktree self-sufficiency:** Every worktree must resolve its dependencies, toolchain, skills, and configuration internally. Tools and tests must not navigate upward with `../..` to locate files in parent checkouts.
- **Physical context verification:** Static file existence does not prove host runtime context injection. Junior runs non-interactive probes (`-p` or headless exec) with low-cost models, asserting visible specification text across root and deep subdirectories.
- **Host verification scope:** Official host CLIs are the primary execution environment. Regular testing covers Antigravity 2.0 project workspaces (`.agents/skills/` and project rules); standalone desktop chat apps without directory awareness are excluded.
- **Local and remote test segregation:** Inner-loop verification (Senior, Junior, Intern) runs only focused tests on changed files (`pytest <file>::<test> -x`). Full-suite regression runs on remote CI, or locally at most once before merge.
- **Bounded file reads:** Pass `StartLine` and `EndLine` (`<= 120` lines per call) when reading existing files with `view_file`.

## Delivery and merge

- **ASD-STE100 specification for all artifacts:** Write all committed artifacts, rule definitions, source code, inline comments, commit messages, and documentation in English using the ASD-STE100 (Simplified Technical English) specification. Keep sentences short: maximum 20 words for instructions and maximum 25 words for descriptions. Use the active voice. Use approved technical words with one meaning per word. Express one topic per sentence. Do not use ambiguous qualifiers (such as "properly", "efficiently", or "seamlessly"). Operational skills and engineering playbooks (`skills/**/SKILL.md`) that contain interactive procedures follow the target repository or author team's language policy.
- **Deliverable format:** Deliver a mergeable PR or a traceable issue, rather than a process-only report. Use issues and PRs as the collaboration bus. Search for an existing similar issue before creating one; update it if it exists.
- **Fail fast left to right:** Put the cheapest and likeliest failure checks first.
- **Review standing authorization:** Resolve a review thread directly after independently verifying it is fixed or obsolete. Do not resolve actionable, ambiguous, or unverified feedback. Automated reviewers may read a redacted GitHub diff rather than source: GitHub can show `"Authorization": f"Bearer ******"` where source has `"Authorization": f"Bearer {token}"`. Check source before judging a report. When a report is false, turn the concern into a falsifiable invariant test rather than merely dismissing it.
- **Weighted review gates:** Each repository defines its own severity weights and blocking thresholds. Read literal `severity: <level>` tags; do not infer severity from prose.
- **Merge when ready:** Once all merge conditions pass, merge and continue from the latest main rather than piling up divergent branches.
- **Delivery state invariant:** Never declare complete, done, or finished while a PR is unmerged or uncommitted. Never declare complete when Stage 1 (Merge to main) has landed without a verified Business Reality Probe. Code landing on main is strictly 'Code Landed'. If a PR is open, report state strictly as 'In review' with the PR URL. If uncommitted or local only, report 'Draft' or 'Local verified'. If merged without a reality probe, report 'Stage 1 Landed'.
- **Anti-exclusion and reality invariant:** Exclusion is not resolution. Marking failing entities as excluded, adding expiring exemptions, or filtering out edge cases to manufacture green checks is a defect. A task is complete only when a physical reality probe confirms non-empty real-world execution.
- **Three-stage deployment prerequisite:** Stage 1 (Merge to main) is a non-bypassable physical prerequisite for Stage 2 and Stage 3. If HEAD is not on main, fail closed and refuse any deployment request.
- **Deploy provenance invariant:** Before dispatching any deploy workflow, physically verify that the target ref or tag contains current branch commits (`git merge-base --is-ancestor HEAD <target>`). Never deploy an older tag as completion evidence for current work.

## Production reservation

The single definition of the production reservation. Other tiers, skills and gates point here.

- **Only a production change returns to the owner.** A production change alters what production runs or stores. Examples: a deploy, promote or apply to production; a production secret write; a manual production data write or deletion. Also a production DNS, route or host change, and a release that cannot be recalled. Code that reaches production with no later owner approval, for example on a schedule, counts too. Each Repo tier names its production paths.
- **Approval:** the owner approves the exact commit SHA or release tag of each production change, and is present when it runs. A "deploy" reply covers only the release that it names. Immediately before a production dispatch, get a live reply from the owner.
- **Self-guard:** an edit that removes, narrows or redefines the production reservation needs the owner's approval of its head SHA. This includes an edit that makes fewer production runs wait for the owner. Examples: the owner-approval check of a deploy path, a production job classification, the settings of a production environment. It also includes an edit that removes or weakens a pin of this section or its dev_env pin test. The same holds for the required check that runs the test.
- **Everything else is the agent's:** in a repository that the owner owns, every merge, staging and canary deploy, and release tag. Also rule text, skills, gate code, data and tests, required-check workflows and release code. Before you merge gate or release code, a read-only reviewer that did not write it checks the head. The review states whether the gate gets stricter, stays equal or gets looser. A looser edit in the self-guard list follows the self-guard.
- **Every other hold is a defect:** a rule, skill, label or gate exit code that holds non-production work for the owner. Do the work, then fix the hold. The dev_env check `ws_owner_scope` fails on it.

# Infra2 SDK Rules

> **Authority boundary:** An AI agent changes this file through its dev_env source. Standing merge authority applies once all gate checks pass. Production reservation applies to package publication.

## SDK role and contract boundaries

The `infra2-sdk` repository provides the single public code contract between `infra2` and external applications (`finance_report`, `truealpha`).

1. **Sole contract carrier:** Stable cross-repository code contracts travel exclusively through published `infra2-sdk` SemVer releases. Applications must not use relative imports into sibling checkouts.
2. **Backward compatibility:** Do not make unversioned breaking changes to exported interfaces. Changes to exported APIs must increment SemVer appropriately and verify downstream consumer compatibility.
3. **Downstream verification:** Before merging changes to SDK interfaces, verify that downstream consumers build and pass tests against the updated contract.
4. **Self-containment:** The SDK must maintain zero runtime dependencies on private harness internals or specific host environments.
<!-- WS_STATIC_END -->
