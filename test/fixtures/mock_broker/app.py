"""Mock Zerodha REST API for E2E tests.

Implements the subset of Zerodha Kite Connect endpoints that OpenAlgo's
broker/zerodha/api/* modules call. State is in-memory and fully controllable
via the /_mock/* admin endpoints.

Run locally:
    pip install fastapi uvicorn
    uvicorn test.fixtures.mock_broker.app:app --port 8001 --reload

All endpoints honour state.token_valid: when False, bearer-authenticated routes
return 401 with the standard Kite error envelope.
"""

from __future__ import annotations

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
# Auth gate helper
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
    form = await request.form()
    order_id = state.next_order_id()
    order = {
        "order_id": order_id,
        "status": "COMPLETE",
        "tradingsymbol": form.get("tradingsymbol", ""),
        "exchange": form.get("exchange", "NSE"),
        "transaction_type": form.get("transaction_type", "BUY"),
        "order_type": form.get("order_type", "MARKET"),
        "quantity": int(form.get("quantity", 0)),
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
def get_historical(
    instrument_token: str,
    interval: str,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    _require_token(authorization)
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
