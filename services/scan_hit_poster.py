"""Stage 1.5 item 6 — scan_hit webhook poster.

Subscribes to the in-process event bus on ``topic="scan_hit"`` and, in
``active`` mode, POSTs each fired symbol into the same Chartink-compatible
simplified-engine webhook the legacy path already feeds. In ``shadow`` mode
(the default), the consumer fires and logs but performs no HTTP request and
no DB mutation — the audit row written by the scanner is left untouched at
``posted_to_engine=0``.

Defaulting to shadow is deliberate: items 5+6 are wired together end-to-end
so the scanner runs to completion, but no live trade signal can leak into
the engine until the operator flips ``SCAN_HIT_POSTER_MODE=active`` in
``.env`` and restarts OpenAlgo. The fail-safe path (HTTP error, timeout,
missing URL) also degrades to "no row update" — we never claim a POST
succeeded that didn't.

Wired from ``app.py`` next to the scanner_service init. See
``test/test_scan_hit_poster.py`` for the contract.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from utils.event_bus import bus as _default_bus
from utils.logging import get_logger

logger = get_logger(__name__)

# Topic the poster subscribes to — kept in sync with
# ``services.scanner_service.ScanHitEvent.topic``.
SCAN_HIT_TOPIC = "scan_hit"

# Modes recognized by ``SCAN_HIT_POSTER_MODE``.
MODE_SHADOW = "shadow"
MODE_ACTIVE = "active"
_VALID_MODES = (MODE_SHADOW, MODE_ACTIVE)


def _env_str(name: str, default: str) -> str:
    val = os.environ.get(name)
    return val if val is not None and val != "" else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "ScanHitPoster: %s=%r not a number, using default %s", name, raw, default
        )
        return default


def _resolve_default_webhook_url() -> str | None:
    """Best-effort: look up the chartink simplified-engine strategy webhook.

    Only used when ``SCAN_HIT_POSTER_WEBHOOK_URL`` is empty. Returns ``None``
    on any failure — the caller treats that as "no URL configured" and
    refuses to POST. We deliberately do NOT hardcode the webhook UUID; it
    lives in the ``strategies`` table and varies per install.
    """
    strategy_name = _env_str(
        "SCAN_HIT_POSTER_STRATEGY_NAME", "chartink_FnO_intraday_buy"
    )
    host_server = _env_str("HOST_SERVER", "http://127.0.0.1:5000").rstrip("/")
    try:
        from database.strategy_db import Strategy

        row = Strategy.query.filter_by(name=strategy_name).first()
    except Exception:
        logger.debug(
            "ScanHitPoster: strategy lookup for %r failed; URL must be set via env",
            strategy_name,
        )
        return None
    if row is None or not row.webhook_id:
        logger.debug(
            "ScanHitPoster: no strategy named %r in DB; URL must be set via env",
            strategy_name,
        )
        return None
    return f"{host_server}/chartink/simplified-stock-engine/{row.webhook_id}"


class ScanHitPoster:
    """Forwards ``scan_hit`` events to the simplified-engine webhook.

    Lifecycle is symmetric with ``ScannerService``:
      * ``start()`` registers the bus subscription. Safe to call once;
        re-calling is a no-op.
      * ``stop()`` unsubscribes. The instance is reusable after stop().

    Mode is captured at construction so tests can drive both paths
    deterministically without touching the global env. Pass ``mode`` and
    ``webhook_url`` explicitly when constructing for tests; production wiring
    in ``app.py`` reads them from env via ``from_env()``.
    """

    def __init__(
        self,
        mode: str = MODE_SHADOW,
        webhook_url: str | None = None,
        request_timeout_seconds: float = 15.0,
        bus: Any = None,
        http_client: Any = None,
    ) -> None:
        if mode not in _VALID_MODES:
            logger.warning(
                "ScanHitPoster: unknown mode %r, falling back to shadow", mode
            )
            mode = MODE_SHADOW
        self.mode = mode
        self.webhook_url = webhook_url or None
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.bus = bus if bus is not None else _default_bus
        # Tests inject a mock that mimics ``httpx.Client.post(url, json=...)``.
        # In production we lazily build a real one per request.
        self._http_client = http_client
        self._subscribed = False

    # -- public lifecycle ---------------------------------------------------

    @classmethod
    def from_env(cls) -> ScanHitPoster:
        """Construct from environment variables. Default mode is shadow."""
        mode = _env_str("SCAN_HIT_POSTER_MODE", MODE_SHADOW).strip().lower()
        url = _env_str("SCAN_HIT_POSTER_WEBHOOK_URL", "").strip() or None
        if url is None:
            url = _resolve_default_webhook_url()
        timeout = _env_float("SCAN_HIT_POSTER_REQUEST_TIMEOUT_SECONDS", 15.0)
        return cls(mode=mode, webhook_url=url, request_timeout_seconds=timeout)

    def start(self) -> None:
        """Subscribe to scan_hit events. Idempotent."""
        if self._subscribed:
            return
        self.bus.subscribe(SCAN_HIT_TOPIC, self._on_scan_hit, name="ScanHitPoster")
        self._subscribed = True
        logger.info(
            "ScanHitPoster started: mode=%s url=%s",
            self.mode,
            "<set>" if self.webhook_url else "<unset>",
        )

    def stop(self) -> None:
        """Unsubscribe from the event bus."""
        if not self._subscribed:
            return
        try:
            self.bus.unsubscribe(SCAN_HIT_TOPIC, self._on_scan_hit)
        except Exception:
            logger.exception("ScanHitPoster: failed to unsubscribe from %s", SCAN_HIT_TOPIC)
        self._subscribed = False
        logger.info("ScanHitPoster stopped")

    # -- event handling -----------------------------------------------------

    def _on_scan_hit(self, event: Any) -> None:
        """Bus subscriber. Routes to shadow or active path based on mode.

        Errors NEVER bubble back into the bus — the EventBus already isolates
        callbacks, but we keep an extra layer here so a future direct-call
        path (tests, or a sync bus) is also safe.
        """
        try:
            symbol = str(getattr(event, "symbol", "") or "").strip()
            symbols = [symbol] if symbol else []
            screener_type = str(getattr(event, "screener_type", "") or "").lower()
            scan_name = str(getattr(event, "scan_name", "") or "")
            scan_result_id = int(getattr(event, "scan_result_id", 0) or 0)
        except Exception:
            logger.exception("ScanHitPoster: malformed scan_hit event")
            return

        if self.mode == MODE_SHADOW:
            logger.info(
                "ScanHitPoster[shadow]: would post symbols=%s scan_name=%r (no POST)",
                symbols, scan_name,
            )
            # Audit row was written by the scanner with posted_to_engine=0.
            # Shadow mode leaves it untouched.
            return

        self._post_active(
            symbols=symbols,
            screener_type=screener_type,
            scan_name=scan_name,
            scan_result_id=scan_result_id,
        )

    # -- active-mode POST ---------------------------------------------------

    def _post_active(
        self,
        symbols: list[str],
        screener_type: str,
        scan_name: str,
        scan_result_id: int,
    ) -> None:
        """Build the Chartink-compatible payload and POST it. Fail-safe."""
        if not self.webhook_url:
            logger.warning(
                "ScanHitPoster[active]: SCAN_HIT_POSTER_WEBHOOK_URL is empty — "
                "refusing to POST. Set it in .env or fall back to shadow."
            )
            return

        payload = self._build_payload(symbols, screener_type, scan_name)

        try:
            response = self._do_post(self.webhook_url, payload)
        except httpx.TimeoutException as e:
            logger.warning(
                "ScanHitPoster[active]: POST to %s timed out (%s) — audit row stays unposted",
                self.webhook_url, e,
            )
            return
        except httpx.HTTPError as e:
            logger.warning(
                "ScanHitPoster[active]: POST to %s failed (%s) — audit row stays unposted",
                self.webhook_url, e,
            )
            return
        except Exception:
            logger.exception(
                "ScanHitPoster[active]: unexpected error posting to %s", self.webhook_url
            )
            return

        status_code = getattr(response, "status_code", 0)
        if not (200 <= int(status_code or 0) < 300):
            logger.warning(
                "ScanHitPoster[active]: engine returned HTTP %s for symbols=%s",
                status_code, symbols,
            )
            return

        if scan_result_id > 0:
            self._mark_posted(scan_result_id)
        logger.info(
            "ScanHitPoster[active]: posted symbols=%s scan_name=%r HTTP %s",
            symbols, payload.get("scan_name"), status_code,
        )

    def _build_payload(
        self, symbols: list[str], screener_type: str, scan_name: str
    ) -> dict[str, str]:
        """Match the Chartink webhook body shape:
        ``{"stocks": "SYM1,SYM2", "scan_name": "..."}``.

        Empty symbol list yields ``{"stocks": ""}`` — the engine accepts
        that shape (see ``parse_chartink_symbols``) and the always-POST
        contract from the SKILL.md fix earlier this session.
        """
        stocks = ",".join(s for s in symbols if s)
        direction = "BUY" if screener_type == "buy" else "SELL"
        name = scan_name or f"{direction} in-house screener"
        return {
            "stocks": stocks,
            "scan_name": name,
        }

    def _do_post(self, url: str, payload: dict[str, str]) -> Any:
        """Single HTTP POST. Uses an injected client (tests) or a one-shot
        ``httpx.Client`` per call (production). We do NOT share the global
        broker pool because broker-tuned timeouts/limits aren't right here.
        """
        if self._http_client is not None:
            return self._http_client.post(url, json=payload)
        with httpx.Client(timeout=self.request_timeout_seconds) as client:
            return client.post(url, json=payload)

    def _mark_posted(self, scan_result_id: int) -> None:
        """Set ``scan_results.posted_to_engine=1`` for ``scan_result_id``.

        Fail-safe: a DB hiccup here must not raise back into the bus or
        change behavior. The HTTP POST already succeeded; the audit row
        just won't reflect the success.
        """
        try:
            from database import scanner_db as sdb

            sess = sdb.db_session
            try:
                row = sess.query(sdb.ScanResult).filter_by(id=scan_result_id).first()
                if row is not None:
                    row.posted_to_engine = 1
                    sess.commit()
            finally:
                sess.remove()
        except Exception:
            logger.exception(
                "ScanHitPoster: failed to mark scan_result %s as posted",
                scan_result_id,
            )
