<!-- Title: <type>: <short summary>   e.g. fix(simplified-engine): stop orphan-exit storm -->

## What & why
<!-- Summary of the change and the problem it solves. -->

Closes #<!-- issue number; required so the issue links to this PR + the merge SHA -->

## Implementation
<!-- Key changes per file; notable design decisions / trade-offs. -->

## If this is a bug fix: what did the previous fix miss?
<!-- Link the prior PR/issue that touched this area and state the gap.
     "First fix in this area" is a valid answer. Required for type:bug PRs. -->

## Tests & verification
<!-- Tests added/updated and how you verified (counts, results). -->

## PR review (required for code-changing PRs)
<!-- Run a review pass BEFORE requesting merge — /code-review in Claude Code,
     or a manual pass against the two guideline docs. Paste the outcome
     (findings fixed / none found). Docs-only PRs may skip this section. -->

**New code** reviewed against [`docs/CODING_GUIDELINES.md`](../docs/CODING_GUIDELINES.md):
- [ ] No silent-drop / partial-success patterns (honest status envelopes, no commit-then-mutate, journal failures at ERROR)
- [ ] Fails loud + fails safe (entries fail closed, exits never gated; degraded ≠ quiet)
- [ ] Runtime-safe (no asyncio under eventlet, NullPool, IST clock injected, idempotent re-runs)
- [ ] Tunables logged in `docs/PARAMETER_LOG.md` (direct to dev); no crypto-material edits

**New/changed tests** reviewed against [`docs/TESTING_GUIDELINES.md`](../docs/TESTING_GUIDELINES.md):
- [ ] No wall-clock/date dependence (clock pinned or raw state asserted)
- [ ] Covers the seam/integration glue and negative paths, not just the happy path
- [ ] Bug fix ⇒ golden-incident regression test that **fails on the pre-fix tree**
- [ ] No weakening of `test/conftest.py` live-DB isolation

Review outcome: <!-- e.g. "/code-review: 2 findings, both fixed (see commits)" or "manual pass, no findings" -->

## Definition of Done
- [ ] Unit/integration tests added or updated, and green
- [ ] **PR review section above completed** (new code + new tests reviewed)
- [ ] **Docker deploy-test passed** (`scripts/docker_smoke.sh`) — paste the `RESULT:` block + image/commit below
- [ ] Docs updated if architectural (`CLAUDE.md` / `docs/SYSTEM_MAP.md`)
- [ ] `docs/PARAMETER_LOG.md` / `strategies/STRATEGY_REGISTRY.md` updated if a tunable/strategy changed
- [ ] Linked issue updated with the commit SHA

<details><summary>Docker deploy-test output</summary>

```
<!-- paste scripts/docker_smoke.sh RESULT block here -->
```
</details>

> Note: `Closes #N` in the PR body auto-closes the linked issue on merge to `dev`
> via the `issue-autoclose.yml` workflow (GitHub's native keyword close only fires
> on the default branch; we close it ourselves).
