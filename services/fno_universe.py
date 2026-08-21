"""Is this symbol tradeable in F&O today? (issue #647)

A **fact**, read once a day from the broker's own instrument dump — not a rule,
not a score, and deliberately not configurable.

Why it needed its own module
----------------------------
The answer already existed, inside ``open15_liquidity_gate.LiquidityGate``. That
class decides whether an option book is too *thin* — a percentile judgement that
ships measuring rather than acting, because its placebo failed on 2026-08-09. So
its first line is ``if not self.enabled: return None``, and switching off the
judgement switched off the fact with it. On 2026-08-19 SAMMAANCAP was recorded
as ``no_option_contracts, enforced: false``, kept in the universe, won a rolling
watch slot, triggered at 09:17:38, and died at ``no_option_contract``.

The two claims are different in kind:

* *"this book is too thin"* can be wrong, and acting on it can cost a good
  trade — measure first.
* *"there are no contracts"* cannot be wrong, and cannot be measured around: a
  symbol with no contracts fills under NO variant, so keeping it in the universe
  produces no data, only an empty slot. Same reasoning as #595's broker-OI
  floor.

Fail OPEN, always
-----------------
Every failure path keeps the WHOLE universe. A real F&O exclusion is one to
three names; a broken master-contract read looks like two hundred. Darkening a
universe because a DB read failed is the #390 shape, where 3 stale symbols of
216 held the entire scanner all session. Hence the plausibility floor below:
it is a safety floor, not a tunable — there is no env var to turn any of this
off, because "does this instrument exist" is not an opinion.
"""

from __future__ import annotations

from utils.logging import get_logger

logger = get_logger(__name__)

# Below this many option underlyings the master contract is not believable — NSE
# stock F&O has held ~180-220 names for years (214 on 2026-08-19). A partial or
# failed instrument download lands here, and the answer is to trust nothing and
# drop nothing.
MIN_PLAUSIBLE_UNDERLYINGS = 50

# degrade reasons, all of which mean "kept everything"
DEGRADE_READ_FAILED = "read_failed"
DEGRADE_IMPLAUSIBLE = "implausible_underlying_count"


def filter_to_fno(
    universe: set[str], underlyings: set[str] | None = None
) -> tuple[set[str], list[str], str | None]:
    """``(kept, dropped, degrade_reason)`` for today's master contract.

    ``dropped`` is sorted so the log line and the decision-log event are stable
    day to day. ``degrade_reason`` is ``None`` on the normal path; when it is
    set, ``kept`` is the input unchanged and ``dropped`` is empty — the caller
    must not treat a degraded answer as "nothing to drop".

    ``underlyings`` is injectable for tests; production passes nothing and the
    set is read from the master contract.
    """
    if underlyings is None:
        try:
            from services.option_liquidity_service import option_underlyings

            underlyings = option_underlyings()
        except Exception:
            logger.critical(
                "fno_universe: master-contract read RAISED — keeping all %d watched "
                "symbols. Nothing is dropped until the instrument dump is readable.",
                len(universe),
            )
            return set(universe), [], DEGRADE_READ_FAILED

    # ``option_underlyings`` swallows its own errors and returns an empty set, so
    # empty and implausible are the same failure wearing two hats.
    if len(underlyings) < MIN_PLAUSIBLE_UNDERLYINGS:
        logger.critical(
            "fno_universe: master contract reports only %d option underlyings "
            "(expected >=%d) — the instrument dump looks broken. Keeping all %d "
            "watched symbols.",
            len(underlyings),
            MIN_PLAUSIBLE_UNDERLYINGS,
            len(universe),
        )
        return set(universe), [], DEGRADE_IMPLAUSIBLE

    kept = {s for s in universe if s in underlyings}
    dropped = sorted(universe - underlyings)
    return kept, dropped, None
