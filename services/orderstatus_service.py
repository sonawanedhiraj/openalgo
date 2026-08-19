import copy
from typing import Any, Dict, Optional, Tuple

from database.analyzer_db import async_log_analyzer
from database.apilog_db import async_log_order
from database.apilog_db import executor as log_executor
from database.auth_db import get_auth_token_broker
from extensions import socketio
from services.mode_service import EffectiveMode, resolve_effective_mode
from services.tradebook_service import get_tradebook
from utils.logging import get_logger

# Initialize logger
logger = get_logger(__name__)


def emit_analyzer_error(request_data: dict[str, Any], error_message: str) -> dict[str, Any]:
    """
    Helper function to emit analyzer error events

    Args:
        request_data: Original request data
        error_message: Error message to emit

    Returns:
        Error response dictionary
    """
    error_response = {"mode": "analyze", "status": "error", "message": error_message}

    # Store complete request data without apikey
    analyzer_request = request_data.copy()
    if "apikey" in analyzer_request:
        del analyzer_request["apikey"]
    analyzer_request["api_type"] = "orderstatus"

    # Log to analyzer database
    log_executor.submit(async_log_analyzer, analyzer_request, error_response, "orderstatus")

    # Emit socket event asynchronously (non-blocking)
    socketio.start_background_task(
        socketio.emit, "analyzer_update", {"request": analyzer_request, "response": error_response}
    )

    return error_response


def get_order_status_with_auth(
    status_data: dict[str, Any], auth_token: str, broker: str, original_data: dict[str, Any]
) -> tuple[bool, dict[str, Any], int]:
    """
    Get status of a specific order using provided auth token.

    Args:
        status_data: Status data containing orderid
        auth_token: Authentication token for the broker API
        broker: Name of the broker
        original_data: Original request data for logging

    Returns:
        Tuple containing:
        - Success status (bool)
        - Response data (dict)
        - HTTP status code (int)
    """
    request_data = copy.deepcopy(original_data)
    if "apikey" in request_data:
        request_data.pop("apikey", None)

    # Read path (issue #440): sandbox source when Analyze is ON, or when the
    # orderid is a sandbox-book order (mixed-mode operation) — otherwise the
    # broker source. is_analyze_mode is kept as a boolean for downstream label
    # formatting (error responses route to analyzer_db vs apilog_db).
    orderid = status_data.get("orderid")
    is_analyze_mode = resolve_effective_mode() is EffectiveMode.SANDBOX
    if not is_analyze_mode and orderid and original_data.get("apikey"):
        from services.sandbox_service import sandbox_order_exists

        is_analyze_mode = sandbox_order_exists(orderid, original_data["apikey"])
    logger.info(
        f"[OrderStatus] Processing order status request - Mode: {'ANALYZE' if is_analyze_mode else 'LIVE'}, OrderID: {orderid}, Broker: {broker}"
    )

    if is_analyze_mode and orderid:
        from services.sandbox_service import sandbox_get_order_status

        logger.info(f"[OrderStatus] Routing to sandbox for order ID {orderid} in analyzer mode")

        api_key = original_data.get("apikey")
        if not api_key:
            return (
                False,
                {
                    "status": "error",
                    "message": "API key required for sandbox mode",
                    "mode": "analyze",
                },
                400,
            )

        return sandbox_get_order_status(status_data, api_key, original_data)

    # For live mode or real orders in analyze mode, fetch from orderbook
    # Both analyze mode and live mode use the same logic - fetch from orderbook
    # This ensures consistent behavior and real data in both modes

    # Use orderbook_service to get order data
    from services.orderbook_service import get_orderbook

    logger.debug(f"[OrderStatus] Fetching orderbook for OrderID: {orderid}")

    success, orderbook_response, status_code = get_orderbook(auth_token=auth_token, broker=broker)

    logger.debug(
        f"[OrderStatus] Orderbook service response: success={success}, status_code={status_code}"
    )

    if not success or orderbook_response.get("status") != "success":
        logger.error(
            f"[OrderStatus] Failed to fetch orderbook - Message: {orderbook_response.get('message', 'Unknown error')}, OrderID: {orderid}"
        )
        error_response = {
            "status": "error",
            "message": orderbook_response.get("message", "Failed to fetch orderbook"),
        }
        if is_analyze_mode:
            error_response["mode"] = "analyze"
            # Log to analyzer database
            log_executor.submit(async_log_analyzer, request_data, error_response, "orderstatus")
            # Emit socket event asynchronously (non-blocking)
            socketio.start_background_task(
                socketio.emit,
                "analyzer_update",
                {"request": request_data, "response": error_response},
            )
        else:
            log_executor.submit(async_log_order, "orderstatus", original_data, error_response)
        return False, error_response, status_code

    # Find the specific order in the orderbook
    order_found = None
    orderbook_data = orderbook_response.get("data", {})

    # Handle different orderbook response structures
    if isinstance(orderbook_data, dict) and "orders" in orderbook_data:
        orders_list = orderbook_data.get("orders", [])
    elif isinstance(orderbook_data, list):
        orders_list = orderbook_data
    else:
        orders_list = []

    logger.info(
        f"[OrderStatus] Searching for OrderID {orderid} in {len(orders_list)} orders from orderbook"
    )

    for idx, order in enumerate(orders_list):
        current_orderid = str(order.get("orderid"))
        if idx < 5:  # Log first 5 order IDs for debugging
            logger.debug(
                f"[OrderStatus] Order {idx + 1}: OrderID={current_orderid}, Symbol={order.get('symbol')}, Status={order.get('order_status')}"
            )

        if current_orderid == str(orderid):
            order_found = order
            logger.info(
                f"[OrderStatus] Found matching order - Symbol: {order.get('symbol')}, Status: {order.get('order_status')}, Price: {order.get('price')}"
            )
            break

    if not order_found:
        logger.warning(
            f"[OrderStatus] Order {orderid} not found in orderbook after searching {len(orders_list)} orders"
        )
        error_response = {"status": "error", "message": f"Order {status_data['orderid']} not found"}
        if is_analyze_mode:
            error_response["mode"] = "analyze"
            # Log to analyzer database
            log_executor.submit(async_log_analyzer, request_data, error_response, "orderstatus")
            # Emit socket event asynchronously (non-blocking)
            socketio.start_background_task(
                socketio.emit,
                "analyzer_update",
                {"request": request_data, "response": error_response},
            )
        else:
            log_executor.submit(async_log_order, "orderstatus", original_data, error_response)
        return False, error_response, 404

    # Resolve the order's fill price (issue #641). Preference order:
    #
    # 1. The orderbook's OWN ``average_price`` — the broker's volume-weighted
    #    average across every partial execution, when the mapper preserves it
    #    (Zerodha does as of #641).
    # 2. The tradebook, volume-weighting ALL trades belonging to the order.
    #    The old code ``break``-ed on the FIRST matching trade, so an order
    #    filled in several partials reported the first partial's price as the
    #    whole order's fill — a Rs912.50 P&L error across two open15 trades on
    #    2026-08-19 (19 trades for 4 orders that day).
    average_price = 0.0
    order_status = order_found.get("order_status", "")

    # Only resolve a fill price for complete orders — a rejected/cancelled
    # order has no fill, and its ``price`` field is the LIMIT we asked for
    # (the #626 rule).
    if order_status.lower() == "complete":
        try:
            native_avg = float(order_found.get("average_price") or 0.0)
        except (TypeError, ValueError):
            native_avg = 0.0
        if native_avg > 0:
            average_price = native_avg
            logger.info(
                f"[OrderStatus] Using orderbook's own weighted average_price for OrderID {orderid}: {average_price}"
            )
        else:
            logger.info("[OrderStatus] Order is complete, deriving average price from tradebook")
            try:
                # Use tradebook_service to get trade data
                success, tradebook_response, status_code = get_tradebook(
                    auth_token=auth_token, broker=broker
                )

                if success and tradebook_response.get("status") == "success":
                    trades_list = tradebook_response.get("data", [])
                    logger.info(
                        f"[OrderStatus] Searching for OrderID {orderid} in {len(trades_list)} trades"
                    )
                    # Volume-weight every trade of this order — one order can
                    # fill in several partials, each its own tradebook row.
                    total_qty = 0.0
                    total_value = 0.0
                    for trade in trades_list:
                        if str(trade.get("orderid")) != str(orderid):
                            continue
                        try:
                            t_qty = float(trade.get("quantity") or 0.0)
                            t_px = float(trade.get("average_price") or 0.0)
                        except (TypeError, ValueError):
                            continue
                        if t_qty > 0 and t_px > 0:
                            total_qty += t_qty
                            total_value += t_qty * t_px
                    if total_qty > 0:
                        average_price = round(total_value / total_qty, 4)
                        logger.info(
                            f"[OrderStatus] Weighted average_price for OrderID {orderid}: {average_price} over qty {total_qty:g}"
                        )
                    else:
                        logger.warning(
                            f"[OrderStatus] No trade found for OrderID {orderid} in tradebook. Available order IDs: {[str(t.get('orderid')) for t in trades_list[:5]]}"
                        )
                else:
                    logger.warning(
                        f"[OrderStatus] Tradebook service call failed: {tradebook_response.get('message', 'Unknown error')}"
                    )
            except Exception as e:
                logger.error(
                    f"[OrderStatus] Exception while fetching tradebook: {e}", exc_info=True
                )
                # Continue without average price if tradebook fetch fails
    else:
        logger.info(
            f"[OrderStatus] Order status '{order_status}' is not complete (open/rejected/other) - skipping average_price fetch"
        )

    # Add average_price to the order data
    order_found["average_price"] = average_price
    logger.debug(f"[OrderStatus] Final average_price set to: {average_price}")

    # Prepare response data
    response_data = {"status": "success", "data": order_found}

    # Add mode indicator for analyze mode
    if is_analyze_mode:
        response_data["mode"] = "analyze"
        logger.info(
            f"[OrderStatus] ANALYZE mode - Preparing response for OrderID {orderid} with status: {order_found.get('order_status')}"
        )

        # Store complete request data without apikey
        analyzer_request = request_data.copy()
        analyzer_request["api_type"] = "orderstatus"

        # Log to analyzer database
        log_executor.submit(async_log_analyzer, analyzer_request, response_data, "orderstatus")
        logger.debug("[OrderStatus] Logged to analyzer database")

        # Emit socket event for toast notification asynchronously (non-blocking)
        socketio.start_background_task(
            socketio.emit,
            "analyzer_update",
            {"request": analyzer_request, "response": response_data},
        )
        logger.debug("[OrderStatus] Emitted socket event for analyzer update")
    else:
        logger.info(
            f"[OrderStatus] LIVE mode - Preparing response for OrderID {orderid} with status: {order_found.get('order_status')}"
        )
        log_executor.submit(async_log_order, "orderstatus", request_data, response_data)
        logger.debug("[OrderStatus] Logged to order database")

    logger.info(
        f"[OrderStatus] Successfully processed order status for OrderID {orderid} - Status: {order_found.get('order_status')}, Symbol: {order_found.get('symbol')}, Average Price: {average_price}"
    )
    return True, response_data, 200


def get_order_status(
    status_data: dict[str, Any],
    api_key: str | None = None,
    auth_token: str | None = None,
    broker: str | None = None,
) -> tuple[bool, dict[str, Any], int]:
    """
    Get status of a specific order.
    Supports both API-based authentication and direct internal calls.

    Args:
        status_data: Status data containing orderid
        api_key: OpenAlgo API key (for API-based calls)
        auth_token: Direct broker authentication token (for internal calls)
        broker: Direct broker name (for internal calls)

    Returns:
        Tuple containing:
        - Success status (bool)
        - Response data (dict)
        - HTTP status code (int)
    """
    original_data = copy.deepcopy(status_data)
    if api_key:
        original_data["apikey"] = api_key

    # Case 1: API-based authentication
    if api_key and not (auth_token and broker):
        # Add API key to status data
        status_data["apikey"] = api_key

        AUTH_TOKEN, broker_name = get_auth_token_broker(api_key)
        if AUTH_TOKEN is None:
            error_response = {"status": "error", "message": "Invalid openalgo apikey"}
            # Skip logging for invalid API keys to prevent database flooding
            return False, error_response, 403

        return get_order_status_with_auth(status_data, AUTH_TOKEN, broker_name, original_data)

    # Case 2: Direct internal call with auth_token and broker
    elif auth_token and broker:
        return get_order_status_with_auth(status_data, auth_token, broker, original_data)

    # Case 3: Invalid parameters
    else:
        error_response = {
            "status": "error",
            "message": "Either api_key or both auth_token and broker must be provided",
        }
        return False, error_response, 400
