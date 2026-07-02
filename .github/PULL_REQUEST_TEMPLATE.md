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

## Definition of Done
- [ ] Unit/integration tests added or updated, and green
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
