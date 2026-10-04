# cas_auction_reversal

Buy the F&O stocks the NSE closing auction pushes **down**, in CNC, and sell them
the next morning at 09:16 (issue #752).

- **Why:** since the Closing Auction Session (2026-08-03) the auction often sets a
  close well below the last continuous print, and the next session undoes it.
- **Strategy-faithful backtest:**
  auction close >= 0.5% below the 15:14 print, top 10/day, Rs50k each, net of CNC
  charges: 280 trades over 40 days, **61.8% net win**,
  +0.286% net per trade, Rs38,599 on Rs5L, max DD -0.61%
  (`backtest/close15/cas_ar_parity.py`). The often-quoted 79% (>= 0.5%) and 91% (>= 1%) are
  R71b's GROSS, UNCAPPED figures: every qualifying stock, no 10/day cap, no charges.
- **Vehicle:** CNC bought in the auction. Stock futures capture only ~4–30% of
  the dip (R71c). The short mirror cannot be traded.
- **Mode:** sandbox by default (`resolve_order_mode`), `deployable: true`. Live
  is an operator decision gated by the pre-registered rule in `PLAN.md`.
- **Code:** `services/cas_auction_reversal_service.py`,
  `database/cas_auction_reversal_db.py`, `blueprints/cas_auction_reversal.py`.
- **Endpoints** (`/cas_auction_reversal/api/`): `status`, `candidates?date=`,
  `trades`, `pause`, `resume`.

See `PLAN.md` for the schedule and the pre-registered gate.
