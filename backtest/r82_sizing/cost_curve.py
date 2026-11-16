"""R82 Phase 3: round-trip cost bar vs notional (Zerodha MIS, corrected exchange 0.00307%, 2.4 bps slippage/side)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "r81_entry"))
import common as C

rows = []
for N in [
    10_000,
    25_000,
    50_000,
    66_667,
    75_000,
    86_000,
    92_000,
    98_000,
    100_000,
    150_000,
    200_000,
    286_000,
    500_000,
    1_000_000,
    1e8,
]:
    c = C.charges_orders([(True, N), (False, N)])
    rows.append(
        {
            "notional": N,
            "charges_rs": round(c["total"], 2),
            "charges_bps": round(1e4 * c["total"] / N, 2),
            "brk_gst_bps": round(1e4 * c["brk"] * 1.18 / N, 2),
            "slip_bps": 4.8,
            "bar_bps": round(1e4 * c["total"] / N + 4.8, 2),
            "bar_rs": round(c["total"] + 0.00048 * N, 1),
        }
    )
for r in rows:
    print(r)
json.dump(rows, open(Path(__file__).parent / "phase3_cost_curve.json", "w"), indent=1)
