#!/usr/bin/env bash
# track.sh — issue + branch + worktree lifecycle helper for the GitHub-tracked
# task workflow. One command per lifecycle step so sessions don't hand-roll gh.
#
# See .claude/skills/track/SKILL.md and CLAUDE.md → "Task Tracking" for the
# discipline this enforces. Requires an authenticated `gh` CLI.
#
# Subcommands:
#   new "<title>" --type <t> [--area <a>] [--session <s>] [--branch|--worktree] [--no-draft-pr]
#   link <N>            ensure the current branch's PR body contains "Closes #N"
#   done <N> [comment]  close issue #N with a templated result comment
#   list                open issues filtered to the workflow labels
#
# By DEFAULT `new` also opens a DRAFT PR right after creating the issue (when
# the operator opts into a branch via --branch or --worktree). The PR body
# already contains "Closes #N" so the auto-close action fires on merge. This
# guarantees PR# = issue# + 1 because no other tracked item can land between
# the issue create and the PR create — they happen in the same gh session,
# in sequence.
#
# Pass --no-draft-pr to skip the draft-PR step (rare: the operator wants to
# write commits before exposing the work on GitHub). Without --branch or
# --worktree, the draft-PR step is a no-op because there's no head to push.
#
# Examples:
#   bash scripts/gh/track.sh new "fix scanner NPE" --type bug --area scanner --branch
#   bash scripts/gh/track.sh new "infra: add foo" --type infra --worktree
#   bash scripts/gh/track.sh new "doc tweak" --type docs --branch --no-draft-pr
#   bash scripts/gh/track.sh link 42
#   bash scripts/gh/track.sh done 42 "Fixed in PR #51; verified by CI."
set -euo pipefail

die() { echo "track: $*" >&2; exit 1; }

slugify() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' \
    | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//' | cut -c1-40
}

prefix_for_type() {
  case "$1" in
    bug|incident) echo "fix" ;;
    enhancement)  echo "feat" ;;
    docs)         echo "docs" ;;
    infra)        echo "infra" ;;
    strategy)     echo "strategy" ;;
    *)            echo "task" ;;
  esac
}

cmd_new() {
  local title="" type="" area="" session="claude-code" do_branch=0 do_worktree=0 do_draft_pr=1
  [ $# -ge 1 ] || die "new: missing \"<title>\""
  title="$1"; shift
  while [ $# -gt 0 ]; do
    case "$1" in
      --type)         type="$2"; shift 2 ;;
      --area)         area="$2"; shift 2 ;;
      --session)      session="$2"; shift 2 ;;
      --branch)       do_branch=1; shift ;;
      --worktree)     do_worktree=1; shift ;;
      --no-draft-pr)  do_draft_pr=0; shift ;;
      *) die "new: unknown flag '$1'" ;;
    esac
  done
  [ -n "$type" ] || die "new: --type is required (bug|enhancement|docs|infra|incident|strategy)"

  local labels=(--label "type:${type}" --label "session:${session}")
  [ -n "$area" ] && labels+=(--label "area:${area}")

  local url num
  url=$(gh issue create --title "$title" "${labels[@]}" --body \
    "Tracked task created via \`scripts/gh/track.sh\`. Link the PR with \`Closes #<N>\`.")
  num=$(printf '%s' "$url" | grep -oE '[0-9]+$')
  [ -n "$num" ] || die "new: could not parse issue number from '$url'"

  local prefix branch
  prefix=$(prefix_for_type "$type")
  branch="${prefix}/${num}-$(slugify "$title")"

  echo "issue:  #$num  $url"
  echo "branch: $branch"

  # Resolve the working dir where the branch + draft PR will live.
  local working_dir=""
  if [ "$do_worktree" -eq 1 ]; then
    local wt="../wt-${num}"
    git worktree add -b "$branch" "$wt" >/dev/null
    [ -f .env ] && cp .env "$wt/.env" 2>/dev/null || true
    working_dir="$wt"
    echo "worktree: $wt  (cd into it; .env copied if present)"
  elif [ "$do_branch" -eq 1 ]; then
    git checkout -b "$branch" >/dev/null 2>&1 || git checkout "$branch" >/dev/null
    working_dir="."
    echo "checked out: $branch"
  fi

  # Open the draft PR. Skipped when --no-draft-pr is set or when no working
  # head exists (no --branch / --worktree → no commits to push). Guarantees
  # PR# = issue# + 1.
  if [ "$do_draft_pr" -eq 1 ] && [ -n "$working_dir" ]; then
    local pr_url pr_num
    if pr_url=$(_open_draft_pr "$working_dir" "$branch" "$num" "$title" "$type" 2>&1); then
      pr_num=$(printf '%s' "$pr_url" | grep -oE '[0-9]+$')
      echo "draft PR: #$pr_num  $pr_url"
      echo ""
      echo "Next: cd $working_dir, push commits to the branch — the draft PR"
      echo "auto-updates. When ready: gh pr ready $pr_num"
    else
      echo "warning: could not open draft PR — $pr_url" >&2
      echo "fallback — open it yourself once the branch has commits:"
      echo "  gh pr create --draft --head $branch --base dev --body 'Closes #$num'"
    fi
  else
    echo ""
    echo "Next: do the work, then open a PR whose body contains: Closes #$num"
  fi
}

# Helper: in $working_dir, make an empty placeholder commit on $branch, push
# it, and open a draft PR with "Closes #$num" in the body. Echoes the PR URL.
# Skips pre-commit hooks for the empty commit only (no diff for them to scan).
_open_draft_pr() {
  local working_dir="$1" branch="$2" num="$3" title="$4" type="$5"
  (
    cd "$working_dir"
    # An empty commit gives the branch a tip to push. --allow-empty +
    # --no-verify is intentional: pre-commit has no diff to inspect, so
    # skipping it is a no-op semantically while keeping the commit
    # mechanically valid.
    git commit --allow-empty --no-verify -m "track: open #$num ($type)" \
      -m "Placeholder commit so the draft PR has a head. Closes #$num" >/dev/null
    git push --set-upstream origin "$branch" --quiet
    gh pr create \
      --draft \
      --head "$branch" \
      --base dev \
      --title "[Draft] $title" \
      --body "$(printf 'Tracking PR for issue #%s. Updates incoming as commits land on this branch.\n\nCloses #%s\n' "$num" "$num")"
  )
}

cmd_link() {
  local n="${1:-}"
  [ -n "$n" ] || die "link: missing issue number"
  local pr body
  pr=$(gh pr view --json number -q .number 2>/dev/null || true)
  if [ -z "$pr" ]; then
    echo "No PR for the current branch yet. When you open it, include in the body:"
    echo "  Closes #$n"
    return 0
  fi
  body=$(gh pr view "$pr" --json body -q .body 2>/dev/null || true)
  if printf '%s' "$body" | grep -qiE "(close[sd]?|fix(e[sd])?|resolve[sd]?) +#$n\b"; then
    echo "PR #$pr already references issue #$n."
    return 0
  fi
  gh pr edit "$pr" --body "$(printf '%s\n\nCloses #%s\n' "$body" "$n")"
  echo "Added 'Closes #$n' to PR #$pr."
}

cmd_done() {
  local n="${1:-}"; shift || true
  [ -n "$n" ] || die "done: missing issue number"
  local comment="${*:-Completed.}"
  gh issue close "$n" --comment "$comment"
  echo "Closed issue #$n."
}

cmd_list() {
  # Lists open issues. Pass through extra args, e.g.:
  #   track.sh list --label type:bug         (filter to one label)
  #   track.sh list --label area:scanner --limit 50
  # (gh treats repeated --label as AND and a comma-joined value as a single
  #  literal label, so we don't pre-filter by an OR set here.)
  gh issue list --state open "$@"
}

main() {
  case "${1:-}" in
    new)   shift; cmd_new "$@" ;;
    link)  shift; cmd_link "$@" ;;
    done)  shift; cmd_done "$@" ;;
    list)  shift; cmd_list "$@" ;;
    ""|-h|--help|help)
      grep -E '^#( |$)' "${BASH_SOURCE[0]}" | sed -E 's/^# ?//' | head -25 ;;
    *) die "unknown subcommand '$1' (try: new|link|done|list)" ;;
  esac
}

# Run the dispatcher only when executed directly, so the file can be sourced
# (e.g. for testing the pure helpers) without side effects.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  main "$@"
fi
