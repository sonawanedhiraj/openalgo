"""Zerodha virtual contract note — ``POST /charges/orders`` (issue #700).

Kite Connect calculates the broker's OWN charges (brokerage, STT/CTT, exchange
turnover, SEBI, stamp duty, GST) for a list of orders. It is a pure calculator:
``order_id`` may be any string and the orders may be historical, so it also
prices legs imported from a Console tradebook export. ``average_price`` must be
non-zero.

Every failure returns ``None`` so the caller can fall back to the modelled
charges and LABEL the row ``modelled`` — a charges figure that silently
degrades to an estimate while claiming to be the broker's is worse than none.
"""

import os

from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)

BROKER_API_URL = os.getenv("BROKER_API_URL", "https://api.kite.trade")

# Kite accepts at most this many orders per call (documented limit is
# generous; keep batches small so one bad leg cannot fail a whole day).
_BATCH = 50


def build_charge_request(
    *,
    order_id: str,
    exchange: str,
    tradingsymbol: str,
    transaction_type: str,
    product: str,
    quantity: int,
    average_price: float,
    order_type: str = "MARKET",
    variety: str = "regular",
) -> dict:
    """One entry of the request body, in Kite's field names."""
    return {
        "order_id": str(order_id),
        "exchange": exchange,
        "tradingsymbol": tradingsymbol,
        "transaction_type": transaction_type.upper(),
        "variety": variety,
        "product": product,
        "order_type": order_type,
        "quantity": int(quantity),
        "average_price": float(average_price),
    }


def get_order_charges(orders: list[dict], auth_token: str) -> dict[str, float] | None:
    """``{order_id: total_charges_inr}`` for ``orders`` (built with
    :func:`build_charge_request`), or ``None`` when the broker could not answer.

    Partial answers are not returned: if any batch fails the whole call is
    ``None``, so a day's legs are either all broker-priced or all modelled and
    the row's ``charges_source`` stays truthful.
    """
    if not orders:
        return {}
    client = get_httpx_client()
    headers = {"X-Kite-Version": "3", "Authorization": f"token {auth_token}"}
    out: dict[str, float] = {}
    try:
        for i in range(0, len(orders), _BATCH):
            batch = orders[i : i + _BATCH]
            response = client.post(f"{BROKER_API_URL}/charges/orders", headers=headers, json=batch)
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") != "success" or not isinstance(payload.get("data"), list):
                logger.warning("charges/orders: unexpected payload shape — falling back")
                return None
            data = payload["data"]
            if len(data) != len(batch):
                logger.warning(
                    "charges/orders: %s orders sent, %s priced — falling back",
                    len(batch),
                    len(data),
                )
                return None
            for req, res in zip(batch, data, strict=True):
                charges = (res or {}).get("charges") or {}
                total = charges.get("total")
                if total is None:
                    return None
                out[str(req["order_id"])] = float(total)
        return out
    except Exception:
        logger.exception("charges/orders call failed — falling back to modelled charges")
        return None
