#!/usr/bin/env bash
# wait_and_merge.sh — self-aware polling watchdog for PR auto-merge.
#
# Polls one or more PRs until their required gates go green, then squash-merges.
# Designed to NOT loop forever on impossible states (the failure mode of the
# 2026-06-21 ad-hoc poller that logged 173 "Merge failed" lines on a PR with
# unresolved merge conflicts).
#
# Three classes of stop conditions, evaluated each poll:
#
#   1. SUCCESS  — `gh pr merge` succeeded OR PR state is already MERGED.
#   2. NEEDS-HUMAN (early exit, alert) —
#        a. mergeStateStatus is DIRTY/BLOCKED (merge conflict or branch
#           protection failure that the watchdog cannot resolve).
#        b. A required gate completed with FAILURE / TIMED_OUT.
#        c. The same non-success state has been observed for STUCK_THRESHOLD
#           consecutive polls (default 5 = 2.5 min on the default 30s
#           interval). Catches "merge keeps failing for the same opaque
#           reason" without burning the full budget.
#   3. TIMEOUT  — overall wall-clock budget elapsed.
#
# In NEEDS-HUMAN or TIMEOUT, the script also cancels stale workflow runs on
# closed PRs (a separate source of runner-queue starvation we kept observing).
#
# All Telegram alerts route through the existing notification_service.notify
# event so the operator gets paged on their phone, not just in the log.
#
# Usage:
#   bash scripts/gh/wait_and_merge.sh <PR>...                    # default 40-min budget
#   bash scripts/gh/wait_and_merge.sh --budget 60 27 28          # 60-min budget
#   bash scripts/gh/wait_and_merge.sh --required "silent-drops,CI: Unit + Integration Tests,CD: Docker + E2E Tests" 27
#   bash scripts/gh/wait_and_merge.sh --dry-run 27               # diagnose only
set -uo pipefail
cd "$(git rev-parse --show-toplevel)"

# ----------------- defaults --------------------------------------------------
LOG=".cache/wait-and-merge.log"
POLL_INTERVAL_S=30
BUDGET_MIN=40
# 30 polls × 30s = 15 min of TRULY FROZEN per-check rollup. Anything shorter
# false-positives on a single self-hosted runner that takes ≈10-15 min to clear
# its queue between PRs. 15 min of zero per-check movement is real stuck.
STUCK_THRESHOLD=30
DRY_RUN=0
REQUIRED_CHECKS=(
    "silent-drops"
    "CI: Unit + Integration Tests"
    "CD: Docker + E2E Tests"
)
PRS=()

# ----------------- args ------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --budget)            BUDGET_MIN=$2; shift 2 ;;
        --interval)          POLL_INTERVAL_S=$2; shift 2 ;;
        --stuck-threshold)   STUCK_THRESHOLD=$2; shift 2 ;;
        --required)          IFS=',' read -ra REQUIRED_CHECKS <<< "$2"; shift 2 ;;
        --dry-run)           DRY_RUN=1; shift ;;
        --log)               LOG=$2; shift 2 ;;
        -h|--help)
            grep -E '^#( |$)' "${BASH_SOURCE[0]}" | sed -E 's/^# ?//' | head -40
            exit 0 ;;
        *)                   PRS+=("$1"); shift ;;
    esac
done

[ ${#PRS[@]} -gt 0 ] || { echo "wait_and_merge: pass at least one PR number" >&2; exit 1; }
mkdir -p "$(dirname "$LOG")"
: > "$LOG"

# ----------------- logging + alerting ----------------------------------------
ts()    { date '+%H:%M:%S'; }
log()   { printf '[%s] %s\n' "$(ts)" "$*" | tee -a "$LOG"; }
warn()  { log "WARN: $*"; }
err()   { log "ERROR: $*"; }

# Best-effort Telegram via the project's notification_service. Failures here
# don't abort the watchdog — operator still sees the log.
notify_op() {
    local subject="$1" body="$2"
    if [ "$DRY_RUN" = "1" ]; then return 0; fi
    # The notification_service module exports a SINGLETON via
    # get_notification_service() — NOT a top-level `notify` function. The
    # earlier draft of this script used `from services.notification_service
    # import notify` which raised ImportError silently in the catch. Use the
    # singleton accessor instead. (Same bug was shipped in the smoke_check
    # and dry_tripwire services and should be patched separately.)
    uv run python -c "
try:
    from services.notification_service import get_notification_service
    get_notification_service().notify('task_complete', f'⚙️ wait_and_merge: $subject\n\n$body')
except Exception as e:
    import sys; print(f'(notify failed: {e})', file=sys.stderr)
" 2>>"$LOG" || true
}

# ----------------- per-PR state probe ----------------------------------------
# Echoes two lines: state-name, then a signature of the required-check rollup.
# state is one of: green / pending / red / conflict / merged.
# signature is a deterministic hash of the per-required-check (status,conclusion)
# tuples. Different signature across polls = checks moved = NOT stuck, even if
# the overall state stayed "pending". This is the fix to the false-positive
# stuck-escalation we saw on the 2026-06-21 retry.
pr_state() {
    local pr=$1
    local out
    out=$(gh pr view "$pr" --json state,mergeable,mergeStateStatus,statusCheckRollup 2>/dev/null) \
        || { printf 'pending\n-\n'; return; }

    printf '%s' "$out" | python -c "
import sys, json
d = json.load(sys.stdin)
if d.get('state') == 'MERGED':
    print('merged'); print('-'); sys.exit(0)
ms = (d.get('mergeStateStatus') or '').upper()
mg = (d.get('mergeable') or '').upper()
if mg == 'CONFLICTING' or ms == 'DIRTY':
    print('conflict'); print('-'); sys.exit(0)
needed = set([$(printf '%s,' "${REQUIRED_CHECKS[@]}" | sed "s/,$//" | sed 's/[^,]*/\"\0\"/g')])
checks = d.get('statusCheckRollup') or []
seen, red, pending = set(), False, False
# Signature: sorted (name, status, conclusion) joined — distinguishes
# QUEUED → IN_PROGRESS → COMPLETED transitions on each individual check.
sig_parts = []
for c in checks:
    name = c.get('name')
    if name not in needed:
        continue
    seen.add(name)
    status = c.get('status') or ''
    concl  = c.get('conclusion') or ''
    sig_parts.append(f'{name}|{status}|{concl}')
    if status != 'COMPLETED':
        pending = True
    elif concl != 'SUCCESS':
        red = True
missing = needed - seen
if red:
    state = 'red'
elif missing or pending:
    state = 'pending'
else:
    state = 'green'
print(state)
print('|'.join(sorted(sig_parts)) or '-')
"
}

# ----------------- merge ----------------------------------------------------
attempt_merge() {
    local pr=$1
    if [ "$DRY_RUN" = "1" ]; then
        log "  DRY-RUN: would merge PR #$pr"
        return 0
    fi
    log "  merging PR #$pr..."
    local out
    if out=$(gh pr merge "$pr" --squash --delete-branch --admin 2>&1); then
        log "  ✅ merged PR #$pr"
        return 0
    fi
    # `gh pr merge` returns the same error text whether the PR was already
    # merged or genuinely failed — parse the message.
    if echo "$out" | grep -qi "was already merged"; then
        log "  ✅ PR #$pr was already merged (treated as success)"
        return 0
    fi
    # Retry without --admin in case branch-protection doesn't allow it.
    if out=$(gh pr merge "$pr" --squash --delete-branch 2>&1); then
        log "  ✅ merged PR #$pr (without admin)"
        return 0
    fi
    if echo "$out" | grep -qi "was already merged"; then
        log "  ✅ PR #$pr was already merged (treated as success)"
        return 0
    fi
    err "  merge failed for PR #$pr: $(echo "$out" | head -2 | tr '\n' ' ')"
    return 1
}

# Cancel stale runs on closed PRs (separate runner-queue-starvation source).
cancel_stale_runs() {
    local stale
    stale=$(gh run list --status in_progress --limit 30 --json databaseId,headBranch,createdAt 2>/dev/null \
        | python -c "
import sys, json, subprocess
runs = json.load(sys.stdin)
for r in runs:
    branch = r.get('headBranch') or ''
    if not branch or branch in ('main', 'dev'): continue
    # Is there an OPEN PR with this head?
    try:
        out = subprocess.check_output(
            ['gh','pr','list','--head',branch,'--state','open','--json','number','-q','.[].number'],
            stderr=subprocess.DEVNULL, text=True
        ).strip()
    except subprocess.CalledProcessError:
        out = ''
    if not out:
        print(r['databaseId'])
")
    if [ -n "$stale" ]; then
        for run_id in $stale; do
            if [ "$DRY_RUN" = "1" ]; then
                log "  DRY-RUN: would cancel stale run $run_id"
            else
                gh run cancel "$run_id" >/dev/null 2>&1 \
                    && log "  cancelled stale run $run_id (no open PR for its branch)" \
                    || true
            fi
        done
    fi
}

# ----------------- main loop -------------------------------------------------
log "=== wait_and_merge starting ==="
log "PRs: ${PRS[*]}"
log "Required checks: ${REQUIRED_CHECKS[*]}"
log "Budget: ${BUDGET_MIN} min  |  interval: ${POLL_INTERVAL_S}s  |  stuck threshold: ${STUCK_THRESHOLD}"
log "Dry run: $DRY_RUN"
log ""

declare -A done_prs prev_sig stuck_count
budget_end=$(( $(date +%s) + BUDGET_MIN * 60 ))
attempt=0
final_status="ok"

while true; do
    attempt=$((attempt + 1))
    now=$(date +%s)
    if [ "$now" -ge "$budget_end" ]; then
        err "TIMEOUT — ${BUDGET_MIN}-min budget exceeded; remaining work needs human review"
        final_status="timeout"
        break
    fi

    log "=== poll attempt $attempt (budget remaining: $(( (budget_end - now) / 60 ))m) ==="
    all_done=1
    for pr in "${PRS[@]}"; do
        if [ "${done_prs[$pr]:-0}" = "1" ]; then continue; fi
        # pr_state returns two lines: state, signature
        state_and_sig=$(pr_state "$pr")
        state=$(printf '%s\n' "$state_and_sig" | sed -n '1p')
        sig=$(printf '%s\n' "$state_and_sig" | sed -n '2p')

        # Stuck-state detection: same SIGNATURE (not just state) N times in a
        # row. A check transitioning QUEUED → IN_PROGRESS → COMPLETED is
        # progress — the overall state stays 'pending' across those polls but
        # the signature changes, resetting stuck_count. Only a TRULY frozen
        # rollup (no check moved at all) counts toward stuck.
        prev=${prev_sig[$pr]:-}
        if [ "$sig" = "$prev" ]; then
            stuck_count[$pr]=$(( ${stuck_count[$pr]:-0} + 1 ))
        else
            stuck_count[$pr]=1
        fi
        prev_sig[$pr]=$sig
        log "PR #$pr: $state (sig-stable for ${stuck_count[$pr]} polls)"

        case "$state" in
            merged)
                log "  ✅ PR #$pr already MERGED"
                done_prs[$pr]=1
                ;;
            green)
                if attempt_merge "$pr"; then done_prs[$pr]=1; fi
                ;;
            conflict)
                err "  ⛔ PR #$pr has merge conflicts (CONFLICTING / DIRTY) — needs human"
                notify_op "PR #$pr needs human" "Merge conflict on PR #$pr. Run: gh pr checkout $pr && git fetch origin dev && git merge origin/dev"
                done_prs[$pr]=1
                final_status="needs_human"
                ;;
            red)
                err "  ⛔ PR #$pr has a RED required check — needs human"
                notify_op "PR #$pr needs human" "A required gate failed on PR #$pr. Inspect: gh pr checks $pr"
                done_prs[$pr]=1
                final_status="needs_human"
                ;;
            pending)
                if [ ${stuck_count[$pr]:-0} -ge $STUCK_THRESHOLD ]; then
                    err "  ⛔ PR #$pr stuck in '$state' for ${stuck_count[$pr]} polls (~$(( stuck_count[$pr] * POLL_INTERVAL_S / 60 ))m) — escalating"
                    notify_op "PR #$pr stuck" "PR #$pr has been in state '$state' for ${stuck_count[$pr]} polls. Inspect: gh pr checks $pr"
                    done_prs[$pr]=1
                    final_status="needs_human"
                else
                    all_done=0
                fi
                ;;
            *)
                warn "  unexpected state '$state' for PR #$pr"
                all_done=0
                ;;
        esac
    done

    if [ $all_done -eq 1 ]; then
        log "all PRs settled"
        break
    fi

    # Opportunistically cancel stale runs every 5 attempts (every 2.5 min).
    if [ $((attempt % 5)) -eq 0 ]; then
        cancel_stale_runs
    fi

    sleep "$POLL_INTERVAL_S"
done

log ""
log "=== summary ==="
log "final status: $final_status"
for pr in "${PRS[@]}"; do
    final=$(gh pr view "$pr" --json state -q .state 2>/dev/null || echo "UNKNOWN")
    log "  PR #$pr: $final"
done
log "log: $LOG"

case "$final_status" in
    ok)          exit 0 ;;
    timeout)     exit 2 ;;
    needs_human) exit 3 ;;
    *)           exit 1 ;;
esac
