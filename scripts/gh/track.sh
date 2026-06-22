#!/usr/bin/env bash
# track.sh — issue + branch + worktree lifecycle helper for the GitHub-tracked
# task workflow. One command per lifecycle step so sessions don't hand-roll gh.
#
# See .claude/skills/track/SKILL.md and CLAUDE.md → "Task Tracking" for the
# discipline this enforces. Requires an authenticated `gh` CLI.
#
# Subcommands:
#   new "<title>" --type <t> [--area <a>] [--session <s>] [--branch] [--worktree]
#   link <N>            ensure the current branch's PR body contains "Closes #N"
#   done <N> [comment]  close issue #N with a templated result comment
#   list                open issues filtered to the workflow labels
#
# Examples:
#   bash scripts/gh/track.sh new "fix scanner NPE" --type bug --area scanner --branch
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
  local title="" type="" area="" session="claude-code" do_branch=0 do_worktree=0
  [ $# -ge 1 ] || die "new: missing \"<title>\""
  title="$1"; shift
  while [ $# -gt 0 ]; do
    case "$1" in
      --type)    type="$2"; shift 2 ;;
      --area)    area="$2"; shift 2 ;;
      --session) session="$2"; shift 2 ;;
      --branch)    do_branch=1; shift ;;
      --worktree)  do_worktree=1; shift ;;
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

  if [ "$do_worktree" -eq 1 ]; then
    local wt="../wt-${num}"
    git worktree add -b "$branch" "$wt" >/dev/null
    [ -f .env ] && cp .env "$wt/.env" 2>/dev/null || true
    echo "worktree: $wt  (cd into it; .env copied if present)"
  elif [ "$do_branch" -eq 1 ]; then
    git checkout -b "$branch" >/dev/null 2>&1 || git checkout "$branch" >/dev/null
    echo "checked out: $branch"
  fi

  echo ""
  echo "Next: do the work, then open a PR whose body contains: Closes #$num"
}

cmd_link() {
  local n="${1:-}"
  [ -n "$n" ] || die "link: missing issue number"
  local pr body title
  pr=$(gh pr view --json number -q .number 2>/dev/null || true)
  if [ -z "$pr" ]; then
    echo "No PR for the current branch yet. When you open it, include in the body:"
    echo "  Closes #$n"
    echo "And prefix the title with [#$n] (so it's visible in the PR list)."
    return 0
  fi
  body=$(gh pr view "$pr" --json body -q .body 2>/dev/null || true)
  title=$(gh pr view "$pr" --json title -q .title 2>/dev/null || true)

  # Body — ensure Closes #N is present (existing link-guard contract).
  if printf '%s' "$body" | grep -qiE "(close[sd]?|fix(e[sd])?|resolve[sd]?) +#$n\b"; then
    echo "PR #$pr body already references issue #$n."
  else
    gh pr edit "$pr" --body "$(printf '%s\n\nCloses #%s\n' "$body" "$n")" >/dev/null
    echo "Added 'Closes #$n' to PR #$pr body."
  fi

  # Title — prepend [#N] if not already there. Match any of:
  #   "[#42]"  "[#42] foo"  "[#42 #43] foo"  "[# 42] foo"
  # Skip the rewrite when the issue number is already prefixed, regardless of
  # surrounding decorations. Otherwise prepend "[#N] ".
  if printf '%s' "$title" | grep -qE "^\[#[0-9 ,#]*\b$n\b[0-9 ,#]*\]"; then
    echo "PR #$pr title already prefixed with #$n."
  else
    local new_title="[#$n] $title"
    gh pr edit "$pr" --title "$new_title" >/dev/null
    echo "Prefixed PR #$pr title with [#$n]: $new_title"
  fi
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
