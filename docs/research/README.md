# Research Index

Durable research persistence for the ai-trade-agent project.
**Tracked in git** — searchable via `grep -r`, browsable on GitHub web UI.
Supersedes the gitignored `outputs/` for anything worth keeping.

## Directory structure

- `journal/` — daily trading journal entries (date-prefixed)
- `strategy/<strategy_name>/` — strategy-specific research, daily reviews, parameter
  discussions, deployment notes
- `adr/` — architecture decision records (single .md per decision)
- `incidents/` — production outage retros + postmortems
- `backtests/` — curated backtest result reports
- `planning/` — roadmaps, capital planning, target-setting work
- `engineering/` — tech debt audits, upstream syncs, CI/CD plumbing
- `data/` — data-coverage notes, historify gaps, broker quirks
- `scanner/` — in-house screener vs Chartink comparison reports, signal-divergence analysis

## Naming convention

`YYYY-MM-DD_short_kebab_topic.md` for date-anchored docs.
For ADRs: `YYYY-MM-DD_short_kebab_decision.md`.
For ongoing strategy notes: `<strategy>_<topic>.md` (no date).

## Process

1. New research → goes here, NOT to `outputs/`.
2. Daily journal → `journal/YYYY-MM-DD.md` (one per trading day).
3. Outages → `incidents/YYYY-MM-DD_incident_name.md` with the engineering:incident-response skill template.
4. ADRs → `adr/YYYY-MM-DD_decision_name.md` with the engineering:architecture skill template.

`outputs/` continues to be the scratchpad for one-shot ephemeral generation
(intermediate analysis, debug dumps) — kept gitignored. Promote to `docs/research/`
when worth persisting.

## Migration log (2026-06-13)

Initial seed migrated 12 files from `outputs/` (copy, not move — originals left in
place to age out). All 12 sources present; none missing. Each migrated file carries
a one-line provenance comment at the top (`<!-- migrated from outputs/… -->`).
