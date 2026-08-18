"""`qty` is a CONTRACT count and must be read as lots (issue #630).

Lot sizes across this universe span 20 to 71,475 — a 3,574x spread — so a bare
contract count is not comparable between two rows of the trades table, and on
2026-08-18 TIINDIA's `800` was read as the lot size, implying a 1-lot cost of
Rs59,040 and casting false doubt on the ATM lot-cost coverage ladder (the real
lot is 200, so one lot was Rs14,760 and the ladder was right).

These tests EXECUTE the page's own `qtyCell` / `lotsLeg` under Node against the
real 2026-08-18 rows rather than pinning strings in `_LOGS_PAGE`. A string pin
proves a function is spelled a certain way; it cannot prove the cell renders
`4 x 200`, which is the whole requirement.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from blueprints.open15_breakout import _LOGS_PAGE

# the page's own definitions, so a change to either one is picked up here
_SHIM = """
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const dash='<span class="muted">&mdash;</span>';
"""


def _page_fns() -> str:
    """`lotsLeg` + `qtyCell` lifted verbatim out of the served page."""
    assert "function lotsLeg(r){" in _LOGS_PAGE, "lotsLeg missing from the page"
    body = _LOGS_PAGE.split("function lotsLeg(r){")[1].split("function pnlCell")[0]
    return "function lotsLeg(r){" + body


def _render(rows: list[dict]) -> list[str]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    script = (
        _SHIM
        + _page_fns()
        + "\nconst rows="
        + json.dumps(rows)
        + ";console.log(JSON.stringify(rows.map(qtyCell)));"
    )
    out = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=60, check=False
    )
    assert out.returncode == 0, f"node failed: {out.stderr}"
    return json.loads(out.stdout)


def test_page_script_parses():
    """`node --check` on the whole page script — a syntax error in an embedded
    string would otherwise only surface in a browser."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    script = _LOGS_PAGE.split("<script>")[-1].split("</script>")[0]
    out = subprocess.run(
        [node, "--input-type=module", "--check"],
        input=script,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert out.returncode == 0, out.stderr


def test_real_2026_08_18_rows_decompose_into_lots():
    """The three rows that produced the confusion, with their journal values."""
    tiindia, motherson, dixon = _render(
        [
            {"symbol": "TIINDIA", "qty": 800, "lotSize": 200, "fill": "paper"},
            {"symbol": "MOTHERSON", "qty": 12300, "lotSize": 6150, "fill": "real"},
            {"symbol": "DIXON", "qty": 150, "lotSize": 50, "fill": "real"},
        ]
    )
    # the contract count still leads — it is what was ordered
    assert tiindia.startswith("800")
    assert "4 &times; 200" in tiindia
    assert "2 &times; 6,150" in motherson
    assert "3 &times; 50" in dixon


def test_sim_and_shadow_keep_zero_on_top():
    """`quantity` is what was ORDERED; for a bucket where nothing was ordered it
    is 0 and the sub-line is the PRICING size. Reversing that would make a
    shadow row read as a position that existed."""
    sim, shadow = _render(
        [
            {"qty": 200, "lotSize": 200, "fill": "sim"},
            {"qty": 800, "lotSize": 200, "fill": "shadow"},
        ]
    )
    for cell in (sim, shadow):
        assert cell.startswith('<span class="muted">0</span>')
    assert "sim 200" in sim and "1 &times; 200" in sim
    assert "shadow 800" in shadow and "4 &times; 200" in shadow


def test_stock_row_is_unchanged():
    """A stock row's `quantity` IS shares and has no lot — it must render
    exactly what it rendered before #630."""
    (cell,) = _render([{"qty": 87, "fill": "real"}])
    assert cell == "87"


def test_missing_qty_still_dashes():
    (cell,) = _render([{"lotSize": 200, "fill": "real"}])
    assert cell == '<span class="muted">&mdash;</span>'


def test_fractional_lot_count_is_never_printed():
    """`qty` not a whole multiple of `lot` means the two came from different
    sources. Print the lot alone rather than a lot count that cannot be true."""
    (cell,) = _render([{"qty": 250, "lotSize": 200, "fill": "real"}])
    assert "1.25" not in cell
    assert "lot 200" in cell
