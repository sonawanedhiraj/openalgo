"""Mock Zerodha REST API for E2E tests.

Implements the subset of Zerodha Kite Connect endpoints that OpenAlgo's
broker/zerodha/api/* modules call. State is in-memory and fully controllable
via the /_mock/* admin endpoints.

Run locally:
    pip install fastapi uvicorn
    uvicorn test.fixtures.mock_broker.app:app --port 8001 --reload

All endpoints honour state.token_valid: when False, bearer-authenticated routes
return 401 with the standard Kite error envelope.

Misbehaving-broker admin endpoints (issue #230)
-----------------------------------------------
The mock starts in happy-path mode. These admin endpoints let a test inject
controlled failure modes that real Zerodha exhibits in production:

* ``POST /_mock/expire_token`` — flip the current token to invalid; next
  authed request returns 401 with the Kite ``TokenException`` envelope.
  (Daily 3 AM IST expiry → WS reinit + ZMQ ``CACHE_INVALIDATE`` — commit
  ``c5f88a8cf``.)
* ``POST /_mock/inject_latency {ms, paths}`` — add ``asyncio.sleep`` before
  responding on matching path prefixes. Send ``{ms: 0}`` to clear.
  (Intermittent 502s / latency under load — boot-burst DuckDB singleton
  bug, commit ``c5b973c91``.)
* ``POST /_mock/drop_ws`` — terminates the active mock WS connection, if
  any. Returns ``501`` with a note if no WS endpoint is exposed (current
  state — the production WS feed is a separate path).
* ``POST /_mock/partial_fill {symbol, ratio}`` — one-shot: the next
  ``POST /orders/regular`` for that symbol returns a partial fill at the
  given ratio (e.g. ``0.5`` → ``filled_quantity = quantity * 0.5``,
  ``status="OPEN"``). Cleared after consumption.
* ``POST /_mock/fail_next {path, status, count}`` — return ``status`` (e.g.
  ``503``) on the next ``count`` requests matching ``path``. One-shot
  (decrements with each match). Lets a test drive the rate-limit /
  ``503`` retry-or-abort-loud path.

All admin endpoints are minimal (in-memory state, ~15-30 LOC each). Default
state preserves the existing happy-path contract so existing tests don't
break.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI(title="Mock Zerodha API", version="1.0.0")


# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------


class _State:
    def __init__(self) -> None:
        self.token_valid: bool = True
        self.balance_equity: float = 1_000_000.0
        self.balance_commodity: float = 0.0
        self.positions: list[dict[str, Any]] = []
        self.orders: list[dict[str, Any]] = []
        self.holdings: list[dict[str, Any]] = []
        self._order_seq: int = 0
        # Misbehaving-broker fixtures (issue #230).
        # ``latency_ms`` applies to any request whose path starts with one of
        # ``latency_paths``. Empty paths list => apply to ALL bearer-authed
        # requests. ``latency_ms == 0`` => disabled (the default).
        self.latency_ms: int = 0
        self.latency_paths: list[str] = []
        # One-shot per-symbol partial-fill plan. The next ``/orders/regular``
        # for that symbol consumes the entry (popped from the dict).
        self.partial_fill_plan: dict[str, float] = {}
        # One-shot fail-next plan. Keyed by path prefix → ``[status, count]``.
        # Decremented on each match; entry removed when count hits 0.
        self.fail_next_plan: dict[str, list[int]] = {}

    def reset(self) -> None:
        self.__init__()

    def next_order_id(self) -> str:
        self._order_seq += 1
        return f"MOCK{self._order_seq:09d}"


state = _State()


# ---------------------------------------------------------------------------
# Admin endpoints  /_mock/*
# ---------------------------------------------------------------------------


class _SetToken(BaseModel):
    valid: bool


class _SetBalance(BaseModel):
    amount: float
    segment: str = "equity"


@app.get("/_mock/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/_mock/token")
def set_token(req: _SetToken) -> dict[str, Any]:
    state.token_valid = req.valid
    return {"status": "ok", "token_valid": state.token_valid}


@app.post("/_mock/balance")
def set_balance(req: _SetBalance) -> dict[str, Any]:
    if req.segment == "commodity":
        state.balance_commodity = req.amount
    else:
        state.balance_equity = req.amount
    return {"status": "ok", "equity": state.balance_equity, "commodity": state.balance_commodity}


@app.post("/_mock/positions/add")
async def add_position(request: Request) -> dict[str, Any]:
    body = await request.json()
    state.positions.append(body)
    return {"status": "ok", "count": len(state.positions)}


@app.post("/_mock/reset")
def reset_state() -> dict[str, str]:
    state.reset()
    return {"status": "reset"}


# ---------------------------------------------------------------------------
# Misbehaving-broker admin endpoints (issue #230)
# ---------------------------------------------------------------------------


class _InjectLatency(BaseModel):
    ms: int
    paths: list[str] = []


class _PartialFill(BaseModel):
    symbol: str
    ratio: float


class _FailNext(BaseModel):
    path: str
    status: int = 503
    count: int = 1


@app.post("/_mock/expire_token")
def expire_token() -> dict[str, Any]:
    """Invalidate the current access token.

    Subsequent bearer-authed requests return the Kite ``TokenException`` 401
    envelope. Mirrors Zerodha's daily 3 AM IST token rotation: from the
    server's perspective the token suddenly stops working mid-session and
    every authed call 401s until the operator re-logs-in.
    """
    state.token_valid = False
    return {"status": "ok", "token_valid": False}


@app.post("/_mock/inject_latency")
def inject_latency(req: _InjectLatency) -> dict[str, Any]:
    """Add ``ms`` of latency before responding to matching paths.

    ``paths`` is a list of path prefixes; an empty list applies the latency
    to ALL bearer-authed requests. Send ``{ms: 0}`` to clear. State is in
    memory — survives until the next ``/_mock/inject_latency`` or
    ``/_mock/reset``.
    """
    state.latency_ms = max(0, int(req.ms))
    state.latency_paths = list(req.paths or [])
    return {"status": "ok", "latency_ms": state.latency_ms, "paths": state.latency_paths}


@app.post("/_mock/drop_ws")
def drop_ws() -> JSONResponse:
    """Terminate any active mock WS connection.

    The current mock broker exposes only the REST surface — the production
    WS feed is a separate Zerodha endpoint (kws.kite.trade) that this mock
    does NOT serve. Return 501 with a note so a test can branch on it; once
    a mock WS endpoint is added here this will switch to "ok".
    """
    return JSONResponse(
        status_code=501,
        content={
            "status": "not_implemented",
            "message": (
                "mock broker has no WS endpoint to drop; WS recovery tests "
                "must inject failure at the recovery-service seam"
            ),
        },
    )


@app.post("/_mock/partial_fill")
def partial_fill(req: _PartialFill) -> dict[str, Any]:
    """Plan the next ``/orders/regular`` for ``symbol`` to return a partial
    fill at the given ratio (e.g. ``0.5`` → ``filled_quantity = qty * 0.5``,
    ``status='OPEN'``). One-shot: consumed (popped) by the next matching
    order. The order envelope returns ``status=success`` with a real
    ``order_id`` — the partial-ness is visible only on the subsequent
    ``GET /orders`` row.
    """
    if not 0.0 <= req.ratio <= 1.0:
        raise HTTPException(status_code=400, detail="ratio must be in [0.0, 1.0]")
    state.partial_fill_plan[req.symbol.upper()] = float(req.ratio)
    return {"status": "ok", "symbol": req.symbol.upper(), "ratio": req.ratio}


@app.post("/_mock/fail_next")
def fail_next(req: _FailNext) -> dict[str, Any]:
    """Return ``status`` on the next ``count`` requests whose path starts
    with ``path``. One-shot per request (decrements ``count``). Lets a test
    drive a rate-limit ``503`` / boot-burst path without coordinating clock
    or load.
    """
    if req.count <= 0:
        raise HTTPException(status_code=400, detail="count must be > 0")
    state.fail_next_plan[req.path] = [int(req.status), int(req.count)]
    return {"status": "ok", "path": req.path, "http_status": req.status, "count": req.count}


# ---------------------------------------------------------------------------
# Auth gate + misbehavior gate helpers
# ---------------------------------------------------------------------------


def _require_token(authorization: str | None) -> None:
    """Raise 401 if no/invalid bearer token or state.token_valid is False."""
    if not authorization or not authorization.startswith("token "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not state.token_valid:
        raise HTTPException(
            status_code=401,
            detail={
                "status": "error",
                "message": "Incorrect api_key or access_token.",
                "error_type": "TokenException",
            },
        )


def _consume_fail_next(path: str) -> None:
    """If a fail-next plan matches ``path``, raise its HTTPException and decrement.

    Called as the first thing inside any authed endpoint, AFTER token validation
    so a 503 plan doesn't shadow a token-expired 401. Matched on path-prefix to
    cover both ``/orders/regular`` and ``/orders/regular/{id}``.
    """
    for plan_path, plan in list(state.fail_next_plan.items()):
        if not path.startswith(plan_path):
            continue
        status_code, remaining = plan
        if remaining <= 1:
            state.fail_next_plan.pop(plan_path, None)
        else:
            state.fail_next_plan[plan_path] = [status_code, remaining - 1]
        raise HTTPException(
            status_code=status_code,
            detail={
                "status": "error",
                "message": f"mock broker returned {status_code} for {path}",
                "error_type": "NetworkException",
            },
        )


async def _maybe_inject_latency(path: str) -> None:
    """If the latency plan matches ``path``, await ``state.latency_ms``."""
    if state.latency_ms <= 0:
        return
    if state.latency_paths and not any(path.startswith(p) for p in state.latency_paths):
        return
    await asyncio.sleep(state.latency_ms / 1000.0)


# ---------------------------------------------------------------------------
# Auth: POST /session/token
# ---------------------------------------------------------------------------


@app.post("/session/token")
async def session_token(request: Request) -> JSONResponse:
    """Exchange request_token for access_token (mock always succeeds)."""
    # OpenAlgo sends form-urlencoded; FastAPI form-parsing needs python-multipart
    # but the body may also arrive as JSON in some test scenarios — accept both.
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)

    _ = body.get("api_key")
    _ = body.get("request_token")
    # Checksum is accepted without validation in test mode

    return JSONResponse(
        {
            "status": "success",
            "data": {
                "access_token": "mock_access_token_12345",
                "user_id": "MOCK001",
                "user_name": "Test User",
                "user_type": "individual",
                "email": "test@mock.local",
                "broker": "ZERODHA",
                "exchanges": ["NSE", "BSE", "NFO", "MCX"],
                "products": ["CNC", "NRML", "MIS"],
                "order_types": ["MARKET", "LIMIT", "SL", "SL-M"],
                "meta": {"demat_consent": "consent"},
                "avatar_url": None,
            },
        }
    )


# ---------------------------------------------------------------------------
# User: GET /user/profile
# ---------------------------------------------------------------------------


@app.get("/user/profile")
def get_profile(authorization: str | None = Header(default=None)) -> JSONResponse:
    _require_token(authorization)
    return JSONResponse(
        {
            "status": "success",
            "data": {
                "user_id": "MOCK001",
                "user_name": "Test User",
                "user_type": "individual",
                "email": "test@mock.local",
                "broker": "ZERODHA",
                "exchanges": ["NSE", "BSE", "NFO", "MCX"],
                "products": ["CNC", "NRML", "MIS"],
                "order_types": ["MARKET", "LIMIT", "SL", "SL-M"],
            },
        }
    )


# ---------------------------------------------------------------------------
# Margins: GET /user/margins
# ---------------------------------------------------------------------------


@app.get("/user/margins")
def get_margins(authorization: str | None = Header(default=None)) -> JSONResponse:
    _require_token(authorization)
    return JSONResponse(
        {
            "status": "success",
            "data": {
                "equity": {
                    "enabled": True,
                    "net": state.balance_equity,
                    "available": {
                        "adhoc_margin": 0.0,
                        "cash": state.balance_equity,
                        "opening_balance": state.balance_equity,
                        "live_balance": state.balance_equity,
                        "collateral": 0.0,
                        "intraday_payin": 0.0,
                    },
                    "utilised": {
                        "debits": 0.0,
                        "exposure": 0.0,
                        "m2m_realised": 0.0,
                        "m2m_unrealised": 0.0,
                        "option_premium": 0.0,
                        "payout": 0.0,
                        "span": 0.0,
                        "holding_sales": 0.0,
                        "turnover": 0.0,
                        "liquid_collateral": 0.0,
                        "stock_collateral": 0.0,
                    },
                },
                "commodity": {
                    "enabled": True,
                    "net": state.balance_commodity,
                    "available": {
                        "adhoc_margin": 0.0,
                        "cash": state.balance_commodity,
                        "opening_balance": state.balance_commodity,
                        "live_balance": state.balance_commodity,
                        "collateral": 0.0,
                        "intraday_payin": 0.0,
                    },
                    "utilised": {
                        "debits": 0.0,
                        "exposure": 0.0,
                        "m2m_realised": 0.0,
                        "m2m_unrealised": 0.0,
                        "option_premium": 0.0,
                        "payout": 0.0,
                        "span": 0.0,
                        "holding_sales": 0.0,
                        "turnover": 0.0,
                        "liquid_collateral": 0.0,
                        "stock_collateral": 0.0,
                    },
                },
            },
        }
    )


# ---------------------------------------------------------------------------
# Positions: GET /portfolio/positions
# ---------------------------------------------------------------------------


@app.get("/portfolio/positions")
def get_positions(authorization: str | None = Header(default=None)) -> JSONResponse:
    _require_token(authorization)
    return JSONResponse(
        {
            "status": "success",
            "data": {
                "net": state.positions,
                "day": state.positions,
            },
        }
    )


# ---------------------------------------------------------------------------
# Holdings: GET /portfolio/holdings
# ---------------------------------------------------------------------------


@app.get("/portfolio/holdings")
def get_holdings(authorization: str | None = Header(default=None)) -> JSONResponse:
    _require_token(authorization)
    return JSONResponse({"status": "success", "data": state.holdings})


# ---------------------------------------------------------------------------
# Orders: GET /orders  POST /orders/regular  PUT /orders/regular/{id}  DELETE
# ---------------------------------------------------------------------------


@app.get("/orders")
def get_orders(authorization: str | None = Header(default=None)) -> JSONResponse:
    _require_token(authorization)
    return JSONResponse({"status": "success", "data": state.orders})


@app.get("/trades")
def get_trades(authorization: str | None = Header(default=None)) -> JSONResponse:
    _require_token(authorization)
    return JSONResponse({"status": "success", "data": []})


@app.post("/orders/regular")
async def place_order(
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    _require_token(authorization)
    _consume_fail_next("/orders/regular")
    await _maybe_inject_latency("/orders/regular")
    form = await request.form()
    order_id = state.next_order_id()
    tradingsymbol = str(form.get("tradingsymbol", ""))
    quantity = int(form.get("quantity", 0))
    # Honour one-shot partial-fill plan (issue #230).
    ratio = state.partial_fill_plan.pop(tradingsymbol.upper(), None)
    if ratio is not None and 0.0 <= ratio < 1.0:
        filled = int(quantity * ratio)
        order_status = "OPEN"
    else:
        filled = quantity
        order_status = "COMPLETE"
    order = {
        "order_id": order_id,
        "status": order_status,
        "tradingsymbol": tradingsymbol,
        "exchange": form.get("exchange", "NSE"),
        "transaction_type": form.get("transaction_type", "BUY"),
        "order_type": form.get("order_type", "MARKET"),
        "quantity": quantity,
        "filled_quantity": filled,
        "pending_quantity": quantity - filled,
        "product": form.get("product", "CNC"),
        "price": float(form.get("price", 0)),
    }
    state.orders.append(order)
    return JSONResponse({"status": "success", "data": {"order_id": order_id}})


@app.put("/orders/regular/{order_id}")
async def modify_order(
    order_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    _require_token(authorization)
    return JSONResponse({"status": "success", "data": {"order_id": order_id}})


@app.delete("/orders/regular/{order_id}")
def cancel_order(
    order_id: str,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    _require_token(authorization)
    state.orders = [o for o in state.orders if o.get("order_id") != order_id]
    return JSONResponse({"status": "success", "data": {"order_id": order_id}})


# ---------------------------------------------------------------------------
# Quotes: GET /quote   GET /quote/ltp   GET /quote/ohlc
# ---------------------------------------------------------------------------


@app.get("/quote/ltp")
def get_ltp(
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    _require_token(authorization)
    instruments = request.query_params.getlist("i")
    data: dict[str, Any] = {inst: {"last_price": 500.0} for inst in instruments}
    return JSONResponse({"status": "success", "data": data})


@app.get("/quote")
def get_quote(
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    _require_token(authorization)
    instruments = request.query_params.getlist("i")
    data: dict[str, Any] = {}
    for inst in instruments:
        data[inst] = {
            "last_price": 500.0,
            "volume": 100000,
            "oi": 0,
            "ohlc": {"open": 495.0, "high": 510.0, "low": 490.0, "close": 498.0},
            "depth": {
                "buy": [{"price": 499.9, "quantity": 100, "orders": 5}],
                "sell": [{"price": 500.1, "quantity": 100, "orders": 5}],
            },
            "last_quantity": 10,
            "average_price": 500.0,
        }
    return JSONResponse({"status": "success", "data": data})


@app.get("/quote/ohlc")
def get_ohlc(
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    _require_token(authorization)
    instruments = request.query_params.getlist("i")
    data: dict[str, Any] = {
        inst: {
            "last_price": 500.0,
            "ohlc": {"open": 495.0, "high": 510.0, "low": 490.0, "close": 498.0},
        }
        for inst in instruments
    }
    return JSONResponse({"status": "success", "data": data})


# ---------------------------------------------------------------------------
# Historical: GET /instruments/historical/{token}/{resolution}
# ---------------------------------------------------------------------------


@app.get("/instruments/historical/{instrument_token}/{interval}")
async def get_historical(
    instrument_token: str,
    interval: str,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    _require_token(authorization)
    _consume_fail_next("/instruments/historical")
    await _maybe_inject_latency("/instruments/historical")
    # Return a minimal set of synthetic candles so the caller doesn't crash
    candles = [
        ["2024-01-02T09:15:00+0530", 500.0, 510.0, 490.0, 505.0, 100000, 0],
        ["2024-01-02T09:20:00+0530", 505.0, 515.0, 500.0, 510.0, 80000, 0],
    ]
    return JSONResponse({"status": "success", "data": {"candles": candles}})


# ---------------------------------------------------------------------------
# Margin calculation: POST /margins/basket  POST /margins/orders
# ---------------------------------------------------------------------------


@app.post("/margins/basket")
async def basket_margin(
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    _require_token(authorization)
    body = await request.json()
    n = len(body) if isinstance(body, list) else 1
    return JSONResponse(
        {
            "status": "success",
            "data": {
                "initial": {"total": n * 10000.0},
                "final": {"total": n * 9000.0},
                "orders": [{"initial": {"total": 10000.0}, "final": {"total": 9000.0}}] * n,
            },
        }
    )


@app.post("/margins/orders")
async def orders_margin(
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    _require_token(authorization)
    body = await request.json()
    n = len(body) if isinstance(body, list) else 1
    return JSONResponse(
        {
            "status": "success",
            "data": [{"total": 10000.0}] * n,
        }
    )
