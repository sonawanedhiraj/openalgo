---
name: track
description: >-
  Track the current unit of work as a GitHub issue and drive its lifecycle
  (create labelled issue, branch/worktree, link the PR with "Closes #N", close
  on completion). Use at the START of any feature / bug fix / docs / backtest
  task, and at the END to close it. Wraps scripts/gh/track.sh.
---

# /track — GitHub-tracked task lifecycle

This skill enforces the repo's task-tracking discipline (see
[`CLAUDE.md`](../../../CLAUDE.md) → "Task Tracking — Every Task Is a GitHub
Issue" and [`docs/TASK_CAPABILITIES.md`](../../../docs/TASK_CAPABILITIES.md)).
All real work is done by `scripts/gh/track.sh`; this skill tells you when and
how to call it.

## When to use

- **At the start of a task** → `new` (open-or-attach an issue, get a branch).
- **After opening the PR** → `link <N>` — writes `Closes #N` to the body AND
  prepends `[#N]` to the title so PR-list scans reveal the linked issue at
  a glance.
- **When the work is merged or otherwise complete** → `done` (close the issue).
  Note: a PR merged to `dev`/`main` with `Closes #N` auto-closes via the
  `issue-autoclose.yml` Action, so `done` is mainly for no-PR/manual work.

## How to run

Always check for an existing issue first, then create if none fits:

```bash
gh issue list --search "<keywords>" --state open      # reuse if a match exists

# Create issue + branch in one step:
bash scripts/gh/track.sh new "<title>" --type <bug|enhancement|docs|infra|incident|strategy> \
    [--area <engine|scanner|preflight|…>] [--session <claude-code|bridge|cowork>] --branch

# For PARALLEL work (multiple concurrent tasks), use a worktree instead of --branch:
bash scripts/gh/track.sh new "<title>" --type <t> --worktree   # creates ../wt-<N>, copies .env

# Link the PR (run after `gh pr create`):
bash scripts/gh/track.sh link <N>

# Close when done (no-PR work, or to add a result comment):
bash scripts/gh/track.sh done <N> "Result summary…"

# List tracked open issues:
bash scripts/gh/track.sh list
```

## Rules this skill encodes

- **Labels**: every issue gets `type:*` (+ `area:*`, `session:*`). Backtest
  rounds use `--type strategy` and should additionally carry
  `strategy:backtest-round` (add via `gh issue edit <N> --add-label`).
- **Branch naming**: `<prefix>/<N>-<slug>` (bug/incident→`fix`,
  enhancement→`feat`, docs→`docs`, infra→`infra`, strategy→`strategy`).
- **`Closes #N` is mandatory** in the PR body for code changes — the
  `link-guard` check blocks otherwise.
- **PR title prefixed `[#N]`** so any PR-list scan reveals the linked issue
  at a glance. `track.sh link <N>` writes this for you. GitHub auto-links
  the `#N` in the title, so it's a one-click jump to the issue.
- **Worktree isolation for parallel tasks** — never run two concurrent
  code-editing tasks in the same checkout (pre-commit stash-collision silently
  reverts edits). `--worktree` creates `../wt-<N>` with `.env` copied in.

## Session-type note

Interactive Claude Code and bridge `claude -p` sessions run the full
create→work→close lifecycle. **Cowork dispatch is read-only**: it may only
`new` an issue (`--session cowork`) describing what it observed, then exit — an
editing session does the code and closes it.
