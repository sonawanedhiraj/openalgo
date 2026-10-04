# cas_auction_reversal

Buy the F&O stocks the NSE closing auction pushes **down**, in CNC, and sell them
the next morning at 09:16 (issue #752).

- **Why:** since the Closing Auction Session (2026-08-03) the auction often sets a
  close well below the last continuous print. The next session undoes it:
  auction >= 0.5% below the 15:14 print → +0.60% by the T+1 09:15 bar close,
  79% win, ~14 names/day, 9/9 weeks positive, about +0.35% net of CNC charges
  (R71b, issue #751).
- **Vehicle:** CNC bought in the auction. Stock futures capture only ~4–30% of
  the dip (R71c). The short mirror cannot be traded.
- **Mode:** sandbox by default (`resolve_order_mode`). The live toggle is
  blocked by `deployable: false` until the live blockers in
  `config_snapshot.json` are cleared.
- **Code:** `services/cas_auction_reversal_service.py`,
  `database/cas_auction_reversal_db.py`, `blueprints/cas_auction_reversal.py`.
- **Endpoints** (`/cas_auction_reversal/api/`): `status`, `candidates?date=`,
  `trades`, `pause`, `resume`.

See `PLAN.md` for the schedule and the pre-registered gate.
