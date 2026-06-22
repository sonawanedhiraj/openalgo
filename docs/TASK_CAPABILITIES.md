# Task Capabilities — what every task may use

Every task in this repo (run from interactive Claude Code, a bridge-spawned
`claude -p`, or proposed by a Cowork dispatch) should be aware of the
capabilities below and pick the most efficient path for the job. This doc is
referenced from [`CLAUDE.md`](../CLAUDE.md) ("Task Tracking" section).

> One-line rule: **every task is a GitHub issue → link the PR with `Closes #N`
> → it auto-closes on merge.** Details in CLAUDE.md.

---

## 1. Claude in Chrome (web UI work)

Use the Claude-in-Chrome MCP tools (`mcp__Claude_in_Chrome__*`) when the task
needs a real browser: navigating the OpenAlgo web UI, clicking through the
analyzer/sandbox pages, completing a Zerodha login the user must finish
manually, or reading a Chartink screener. Prefer DOM-aware tools
(`navigate`, `find`, `javascript_tool`) over pixel clicks. The extension must be
connected; if it is not, ask the user to connect it rather than falling back to
slower desktop control.

## 2. Cowork to launch apps

Use Cowork / computer-use to open and arrange the desktop apps a task depends
on — **Chrome, Docker Desktop, the OpenAlgo app, and the bridge server** — when
they are not already running. Bringing an app to the foreground is a read-level
action; check first with a screenshot or `list_granted_applications` before
asserting an app's state. Do not Start-Process Docker Desktop if it is already
running (it forces a restart — see the CI Docker memory).

## 3. Bridge fallback when Claude Code is limited inside Cowork

A Cowork dispatch runs in a sandboxed Linux shell with **no access to the
Windows working tree, no `git`, and no commit ability**. When a task needs to
actually edit code, run the test suite, or restart OpenAlgo on the Windows
machine, route it through the **bridge** (`bridge/server.py`, port 5001), which
spawns a real `claude` CLI with `cwd=PROJECT_ROOT` (so it inherits `CLAUDE.md`):

- `POST /run` — run any prompt via Claude Code on the Windows box
- `POST /fix-bug` — send an error; Claude fixes + tests
- `POST /run-tests` — pytest, optional auto-fix
- `POST /restart-app` — restart OpenAlgo

Call it from browser JS on any OpenAlgo tab (see CLAUDE.md → "Cowork ↔ Claude
Code Bridge Server").

## 4. Independence (worktree isolation)

Every task must be runnable independently and in parallel with others. **Run
concurrent code-editing tasks in their own `git worktree`** — either
`Agent(isolation: "worktree")` or `git worktree add ../wt-<issue> <branch>`.
This is load-bearing: two agents committing in the *same* checkout deadlock
`pre-commit` (an internal git-stash collision) and the killed stash **silently
reverts your working-tree edits**. A worktree gives each task its own index and
sidesteps the deadlock entirely.

Recovery if an edit vanishes mid-run: recover from `.cache/pre-commit/patch*` →
`git apply --cached <patch>` → run `uv run ruff` manually → `git commit
--no-verify` → push immediately.

A fresh worktree has no `.env` (it is gitignored) — copy it in
(`cp ../openalgo/.env .`) before importing anything that reads
`API_KEY_PEPPER`; `test/conftest.py` still redirects DBs to a temp dir.

## 5. Respect the repo's CI/CD rules

Branches PR into `dev` (then `dev` → `main`). The required checks are:

- **`silent-drops`** — custom Semgrep ERROR rules (`.semgrep/silent-drops.yml`);
  must be 0 findings.
- **`CI: Unit + Integration Tests`** — `pytest -n auto` on the self-hosted runner.
- **`CD: Docker + E2E Tests`** — Docker build → container boot → E2E.
- **`link-guard`** (this workflow) — a code-changing PR must contain `Closes #N`.

Pure docs/`*.md` changes are path-ignored by the CI/CD workflow by design, so
they skip the expensive unit/Docker jobs.

## 6. A task can run its own CI/CD

You don't have to wait for GitHub to validate work:

- **Lint/format:** `uv run ruff check .` / `uv run ruff format .`
- **Custom gate:** `uvx semgrep --config .semgrep/silent-drops.yml --severity ERROR services/ blueprints/ sandbox/ restx_api/`
- **Tests:** `uv run pytest test/ -n auto --ignore=test/e2e`
- **Full Docker E2E locally:** `docker compose up -d --build` then hit the health
  endpoint and run the E2E suite, mirroring the `CD` job.

Pushing the branch also fires the real Actions; both paths are valid.

## 7. Cowork read-only carve-out

Cowork scheduled/dispatch tasks run **read-only on this repo's code** — they
must never edit source, `git add`, or commit (see CLAUDE.md → "Scheduled Tasks
Audit-Trail Policy"). Their role in this workflow is to **observe and propose**:
open a `session:cowork`-labelled issue (or append to
[`audit/proposed_fixes.jsonl`](../audit/proposed_fixes.jsonl)) describing the
problem, then exit. A Claude Code or bridge session picks that up, does the code
change, links `Closes #N`, and closes it. The create→work→close lifecycle is
**asymmetric** across session types — Cowork opens, an editing session closes.
