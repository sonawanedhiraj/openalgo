<!-- migrated from outputs/2026-06-13_ruff_1535_triage.md on 2026-06-13 | summary: Ruff Debt Triage â€” 1613 errors on `dev` -->

# Ruff Debt Triage â€” 1613 errors on `dev`

**Date:** 2026-06-13 Â· **Author:** Claude Code (analysis only) Â· **Scope:** `uv run ruff check .` repo-wide
**Status:** READ-ONLY ANALYSIS. No code changed, no `--fix` run, no commits. Phase-1 config proposal at the bottom is a *proposal* â€” not applied.

> Raw data captured at `outputs/ruff_full.json` (1613 records). Aggregation script: `outputs/_ruff_triage_analysis.py` (analysis helper, not production code).

---

## 1. Executive summary

- **Total: 1613 errors** (the "1535" figure has drifted up ~78 since it was measured; same debt, slightly grown).
- Ruff already **ignores** the four highest-volume rules that would otherwise dominate: `E501` (line length), `F401` (unused import), `E402` (import-not-top), `B008`. So these 1613 are *net of* the noisy ones. Bumping `line-length` to 120 changes **nothing** â€” `E501` is already off.
- **Fixability:** 432 are safe-auto-fixable (`[*]`), 441 more only via `--unsafe-fixes` (`[-]`), the remainder need manual judgment.
- **53% of all errors live in `broker/`** â€” 30+ vendored-style broker integrations, much of it copy-pasted across the XTS family (compositedge / fivepaisaxts / ibulls share identical line numbers) and generated protobuf.

### Top 5 rules by count
| Rule | Count | What it is |
| --- | --- | --- |
| `UP035` | 314 | `typing.Dict`â†’`dict` deprecated-import (PEP 585) |
| `W293` | 245 | blank line contains whitespace |
| `F841` | 192 | local variable assigned but never used |
| `F821` | 173 | undefined name (**mostly false positives** â€” see Â§6) |
| `B904` | 134 | `raise` without `from` inside `except` |

### Top 5 files by count
| Count | File | Type |
| --- | --- | --- |
| 46 | `services/telegram_bot_service.py` | production (live, running now) |
| 45 | `broker/nubra/api/nubrawebsocket.py` | broker |
| 43 | `services/telegram_bot_service_fixed.py` | **dead duplicate** |
| 41 | `broker/upstox/streaming/MarketDataFeedV3_pb2.py` | **generated protobuf** |
| 41 | `services/telegram_bot_service_v2.py` | **dead duplicate** |

### Recommended phased approach (detail in Â§8)
1. **Phase 1 â€” config excludes only.** Drops ~293 errors (-18%), **zero code change, zero risk.** Exclude generated protobuf, `examples/`, untracked root scratch `_*.py`, and the two dead telegram duplicates.
2. **Phase 2 â€” Tier A auto-fix** (`ruff format` + safe `--fix`): whitespace, import order, trivial. ~430 errors, mechanical, gated by the full mocked test suite.
3. **Phase 3 â€” Tier B** typing modernization (`UP035`/`UP006`/`UP045`) + C4 cosmetics â€” bulk, OR a one-line rule-disable decision.
4. **Phase 4 â€” Tier C manual.** The genuinely bug-catching pyflakes rules (`F823`, `F402`, `F811`, `F601`, `B023`, plus the real subset of `F821`/`F841`) reviewed per-occurrence. This is the only phase with real correctness value.

---

## 2. Where the errors live (bucket analysis)

| Count | % | Bucket | Disposition |
| --- | --- | --- | --- |
| 861 | 53.4% | `broker/` production integrations | mixed â€” fix the live brokers, exclude generated |
| 400 | 24.8% | production core (`services`, `blueprints`, `database`, `utils`, `restx_api`, `sandbox`, `websocket_proxy`, `backtest`) | the real target |
| 84 | 5.2% | `examples/` | **exclude** (sample scripts, not shipped) |
| 84 | 5.2% | telegram dead duplicates (`_fixed`, `_v2`) | **exclude or delete** |
| 75 | 4.6% | root scratch `_*.py` | **exclude** (untracked â€” not even in git) |
| 59 | 3.7% | tests | keep linting (low count) |
| 50 | 3.1% | generated protobuf (`*_pb2.py`, `*/protos/`) | **exclude** (machine-generated) |

**Config-excludable with zero risk: 75 + 50 + 84 + 84 = 293 errors (18%).**

---

## 3. Per-rule triage table

Tier legend â€” **A**: trivially safe (whitespace / import-order / formatting; `ruff format` or safe `--fix`). **B**: behavior-preserving but verify with tests. **C**: behavior-changing or needs per-occurrence judgment. **D**: pedantic/arguable â€” consider disabling the rule instead of fixing.

| Rule | Count | Fix | Tier | Recommended action | Risk |
| --- | --- | --- | --- | --- | --- |
| `UP035` deprecated-import | 314 | unsafe | B/D | Bulk-fix in Phase 3 **or** disable â€” pure PEP585 churn across 30 brokers, no behavior change | Low |
| `W293` blank-line-whitespace | 245 | unsafe | A | `ruff format` | None |
| `F841` unused-variable | 192 | unsafe | C | Manual â€” many are intentional (`_ = x`, side-effect calls, debugger refs); do **not** bulk-delete | Med |
| `F821` undefined-name | 173 | none | C | **~155 false positives** (guarded telegram imports + generated pb2); ~15 real latent bugs â€” review Â§6 | Med (real subset) |
| `B904` raise-without-from | 134 | unsafe | C/D | Decide: adds `from e` (better tracebacks, no behavior change) or disable as noise | Low |
| `I001` unsorted-imports | 119 | safe | A | `ruff check --fix` | None |
| `C408` unnecessary-dict-call | 85 | none | B/D | Cosmetic `dict()`â†’`{}`; bulk or disable | Low |
| `B007` unused-loop-var | 61 | none | C | Rename to `_var`; mostly scratch + backtest | Low |
| `UP006` non-pep585-annotation | 34 | safe | B | With `UP035` in Phase 3 | Low |
| `E702` semicolon-statements | 33 | none | C | Mostly untracked scratch `_*.py` â†’ vanishes with Phase 1 | Low |
| `F541` f-string-no-placeholder | 25 | safe | A | `--fix` (drops the `f` prefix) | None |
| `E701` colon-statements | 23 | none | C | Mostly scratch scripts | Low |
| `E712` ==True/==False | 17 | none | B | **Careful** â€” truthiness change can bite numpy/pandas `Series`; review each | Med |
| `W291` trailing-whitespace | 15 | unsafe | A | `ruff format` | None |
| `E741` ambiguous-name (`l`/`I`) | 13 | none | C | Rename; mostly scratch | Low |
| `B905` zip-without-strict | 12 | none | C | Add `strict=False` to preserve behavior (don't default `True`) | Low |
| `UP045` non-pep604-optional | 10 | safe | B | With Phase 3 | Low |
| `E401` multiple-imports | 9 | safe | A | `--fix`; mostly scratch | None |
| `UP015` redundant-open-mode | 9 | safe | A | `--fix` | None |
| `C401` unnecessary-generator-set | 9 | none | B | Cosmetic | Low |
| `UP017` datetime-utc-alias | 9 | safe | A/B | `--fix` (`timezone.utc`â†’`datetime.UTC`) | Low |
| `B025` duplicate-except | 8 | none | C | **Real smell** â€” duplicate `except Exception` is dead code; review | Med |
| `W292` no-newline-eof | 8 | safe | A | `ruff format` | None |
| `F823` ref-before-assignment | 7 | none | C | **Real bug smell** â€” review each (XTS-family `session`) | High |
| `F402` import-shadowed-by-loop | 6 | none | C | **Real bug smell** â€” loop var clobbers import; review | High |
| `E722` bare-except | 6 | none | C | Narrow to `except Exception`; all in `deltaexchange` | Med |
| `F811` redefined-while-unused | 5 | none | C | **Real smell** â€” duplicate import/def; review | Med |
| `UP031` printf-format | 3 | none | B | Scratch scripts | Low |
| `C416` unnecessary-comprehension | 3 | safe | B | `--fix` | Low |
| `UP009` utf8-declaration | 3 | safe | A | All generated pb2 â†’ vanishes with Phase 1 | None |
| `B009` getattr-constant | 3 | safe | B | `--fix` | Low |
| `UP041` timeout-error-alias | 2 | safe | B | `--fix` | Low |
| `UP037` quoted-annotation | 2 | safe | A | `--fix` | None |
| `UP024` os-error-alias | 2 | safe | B | `--fix` | Low |
| `F601` repeated-dict-key | 2 | none | C | **Real bug** â€” second key wins silently (`examples/ltp_example.py`) | Med |
| `E711` none-comparison | 2 | none | B | `== None`â†’`is None` | Low |
| `C420` dict-comprehension | 2 | safe | B | `--fix` | Low |
| `C414` double-cast | 2 | none | B | Cosmetic | Low |
| `B023` loop-var-binding | 2 | none | C | **Real bug** â€” closure captures loop var late (`indmoney`) | High |
| `UP032`/`C402`/`C405`/`B010` | 1 each | mixed | A/B | `--fix` | Low |

---

## 4. Per-file triage (top 20)

| Count | File | Type | Action |
| --- | --- | --- | --- |
| 46 | `services/telegram_bot_service.py` | production (running) | fix in Phase 2â€“4; 38 are `F821` false-positives from guarded `python-telegram-bot` imports |
| 45 | `broker/nubra/api/nubrawebsocket.py` | broker production | fix (typing/whitespace heavy) |
| 43 | `services/telegram_bot_service_fixed.py` | **dead duplicate** | **exclude/delete** |
| 41 | `broker/upstox/streaming/MarketDataFeedV3_pb2.py` | **generated** | **exclude** |
| 41 | `services/telegram_bot_service_v2.py` | **dead duplicate** | **exclude/delete** |
| 39 | `broker/nubra/mapping/order_data.py` | broker | fix |
| 37 | `broker/nubra/api/order_api.py` | broker | fix |
| 31 | `broker/dhan_sandbox/streaming/dhan_sandbox_adapter.py` | broker | fix |
| 30 | `broker/nubra/api/data.py` | broker | fix |
| 26 | `_deepen_backfill.py` | **untracked scratch** | **exclude** |
| 26 | `examples/python/Nifty OI Charts.py` | example | **exclude** |
| 23 | `broker/zerodha/database/master_contract_db.py` | broker (active broker) | fix â€” priority |
| 19 | `broker/nubra/database/master_contract_db.py` | broker | fix |
| 18 | `broker/groww/api/order_api.py` | broker | fix; has 7 `F821` â€” review for real bugs |
| 17 | `websocket_proxy/server.py` | production core | fix; 4 `F821` to review |
| 16 | `broker/nubra/mapping/transform_data.py` | broker | fix |
| 16 | `broker/nubra/streaming/nubra_adapter.py` | broker | fix |
| 13 | `scripts/bench_parity_opengreeks.py` | dev script | low priority |
| 13 | `services/flow_executor_service.py` | production core | fix â€” priority |
| 11 | `examples/python/backtesting_vectorbt.py` | example | **exclude** |

---

## 5. Configuration recommendations (reduce count with NO code change)

Current `[tool.ruff]`: `line-length=100`, `select=[E,F,W,I,B,C4,UP]`, `ignore=[E501,B008,E402,F401]`, `exclude=[.venv, frontend, node_modules, __pycache__, *.pyc, db, log, strategies]`.

**Add to `exclude` (risk 0 â€” these are generated / sample / untracked / dead):**
- `examples` â€” sample scripts, never shipped (-84)
- `**/*_pb2.py` and `broker/*/protos` â€” machine-generated protobuf (-50)
- `_*.py` at repo root â€” untracked scratch/diagnostic scripts, not in git (-75)
- `services/telegram_bot_service_fixed.py`, `services/telegram_bot_service_v2.py` â€” dead duplicates of the live file (-84) *(better: delete them in a follow-up; exclude now)*

**Do NOT bother:**
- Bumping `line-length` to 120 â€” `E501` is already in `ignore`, so it has no effect.
- Trimming the `select` list wholesale â€” `E/F/W` are the bug-catching core; keep them. Only `UP035`/`B904` are defensible candidates for `ignore` (see Â§3), and that's a Phase-3 *decision*, not a Phase-1 freebie.

---

## 6. The F821 caveat (important â€” don't bulk-touch)

173 `F821` undefined-name looks alarming but **breaks down as ~90% false positives:**
- `ContextTypes` (49) + `Update` (49) = 98 â€” imported under guarded/`TYPE_CHECKING` patterns from `python-telegram-bot` in the 3 telegram files. The live bot **runs fine right now** (PID confirms), so these are lint-time-only.
- `_MARKETINFO_SEGMENTSTATUSENTRY`, `_FEEDRESPONSE_FEEDSENTRY`, `_TYPE`, `_REQUESTMODE`, `SEGMENT_*` (~20) â€” internal references inside generated pb2 files (vanish with Phase-1 exclude).

**The ~15 that ARE worth a look** (real latent bugs in rarely-exercised broker error paths): `result` (3), `response`, `openalgo_api` (3), `asyncio` (3), `send_message`, `unsubscribe_update`, `fake_candles` â€” e.g. `broker/aliceblue/api/order_api.py:210` references `result`/`response` in an `except` block where they were never assigned. These would raise `NameError` if that path executes. **Manual, per-occurrence, Phase 4.**

---

## 7. The genuinely bug-catching rules (the real prize)

If the goal is correctness rather than cosmetics, these ~30 occurrences are where the value is â€” every one is a potential latent defect, all in broker code that rarely runs:
- `F823` (7) ref-before-assignment Â· `F402` (6) import shadowed by loop var Â· `F811` (5) redefinition Â· `F601` (2) silently-dropped dict key Â· `B023` (2) loop-var closure capture Â· `B025` (8) dead duplicate-except Â· the real `F821` subset (~15).

Everything else (UP*, W*, C4*, I001, B904, B007) is style/modernization churn with little correctness payoff.

---

## 8. Phased plan

| Phase | Scope | Errors cleared | Risk | Effort | Gate |
| --- | --- | --- | --- | --- | --- |
| **1** | Config excludes only (Â§5) | ~293 (-18%) | **None** â€” no file touched | 10 min | review diff of `pyproject.toml` only |
| **2** | Tier A: `ruff format` + safe `--fix` (`I001`, `F541`, whitespace, `W292`, safe `UP*`) | ~430 | Very low (mechanical) | 1â€“2 h | full mocked E2E + unit suite green |
| **3** | Tier B: typing modernization (`UP035`+`UP006`+`UP045` = 358) + C4 cosmetics â€” **or** `ignore` the rules | ~450 | Low | 2â€“3 h (or 5 min if disabling) | full suite |
| **4** | Tier C: real-bug pyflakes (Â§7) + `F841`/`E712`/`B905`/`E722` per-occurrence + `B904` decision | ~600 | Med-High (touches logic) | 1â€“2 days, reviewed | full suite + manual review each |

After Phases 1â€“3 the count drops from 1613 to roughly **~440**, almost all of which is Phase-4 manual-review territory. The Quality-gate workflow could be made green much sooner by combining Phase 1 + a `--baseline-commit` so only *new* violations block (defer the backlog without fixing it).

**Recommendation:** ship Phase 1 now (free), do Phase 2 next (mechanical, test-gated), make a team call on Phase 3 (fix vs. disable `UP035`/`B904`), and treat Phase 4 as a real bug-hunt â€” not a lint sweep.

---

## 9. Phase 1 concrete proposal (DO NOT APPLY â€” review first)

Replace the `exclude` list in `[tool.ruff]` of `pyproject.toml`. **Config-only; no `.py` file is touched.** Expected effect: 1613 â†’ ~1320.

```toml
[tool.ruff]
line-length = 100
target-version = "py312"
exclude = [
    # --- existing ---
    ".venv",
    "frontend",
    "node_modules",
    "__pycache__",
    "*.pyc",
    "db",
    "log",
    "strategies",
    # --- Phase 1 additions (all zero-risk: generated / sample / untracked / dead) ---
    "examples",            # sample scripts, never shipped              (-84)
    "**/*_pb2.py",         # machine-generated protobuf                 (part of -50)
    "broker/*/protos",     # machine-generated protobuf package          (part of -50)
    "_*.py",               # untracked root scratch/diagnostic scripts  (-75)
    # Dead duplicates of the live services/telegram_bot_service.py.
    # Excluding now; recommend DELETING in a follow-up commit (they are
    # committed but superseded â€” verify no import references first).
    "services/telegram_bot_service_fixed.py",   # (-43)
    "services/telegram_bot_service_v2.py",       # (-41)
]

[tool.ruff.lint]
# UNCHANGED in Phase 1. Listed here only so the Phase-3 decision is visible:
#   - To defer the 314 UP035 + 134 B904 without fixing, add "UP035", "B904"
#     to `ignore` below. That is a Phase-3 *policy* call, NOT part of the
#     zero-risk Phase-1 sweep â€” left commented intentionally.
select = ["E", "F", "W", "I", "B", "C4", "UP"]
ignore = [
    "E501",   # line length (handled by formatter)
    "B008",   # function call in argument defaults
    "E402",   # module import not at top
    "F401",   # imported but unused
    # "UP035",  # <-- Phase 3 candidate (PEP585 import churn, 314 hits)
    # "B904",   # <-- Phase 3 candidate (raise-from noise, 134 hits)
]

[tool.ruff.lint.isort]
known-first-party = [
    "broker", "blueprints", "database", "services", "utils",
    "restx_api", "extensions", "limiter", "cors", "csp",
]
```

**Validation step (read-only) after applying â€” do NOT auto-fix:**
```bash
uv run ruff check . --statistics   # confirm count dropped to ~1320, no new rules
```

**Caveat on the two telegram excludes:** before *deleting* `_fixed`/`_v2` (follow-up, not Phase 1), grep for imports â€” `grep -rn "telegram_bot_service_fixed\|telegram_bot_service_v2" --include=*.py .` â€” to confirm nothing references them. Excluding from lint is safe regardless; deletion needs that check.
```
