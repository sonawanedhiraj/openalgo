"""
Master Contract Cache Hook
Automatically loads symbols into memory cache after successful master contract download
"""

import time

from extensions import socketio
from utils.logging import get_logger

logger = get_logger(__name__)


# Canonical NSE_INDEX symbols that downstream consumers (scanner_presubscribe,
# sector_follow mapped indices, ws_recovery default universe, regime
# pre-subscriber) rely on. Issue #241: the 11 sectoral indices on the right
# (plus broad-market companions on the left) must resolve to a token on
# ``NSE_INDEX`` after every master-contract sync — if they don't, scanner
# pre-subscribe fails closed (warnings flood) and ws_recovery historical
# backfill drops bars for the affected symbols.
#
# Sourced from:
#   * sector_follow_cap5_vol/sector_map.json mapped indices, and
#   * the issue #241 NSE_INDEX warning set (the 11 sectorals).
#
# Kept here (and NOT imported from scanner_presubscribe.INDEX_SYMBOLS) so the
# load-after-sync verification has its own intent-stable list — the
# INDEX_SYMBOLS set is allowed to grow more permissively for future
# routing-correctness; this set is the operational floor we MUST keep
# resolvable after every sync.
_REQUIRED_NSE_INDEX_SYMBOLS: tuple[str, ...] = (
    # sector_follow mapped sector indices (8)
    "FINNIFTY",
    "NIFTY",
    "NIFTYAUTO",
    "NIFTYFMCG",
    "NIFTYIT",
    "NIFTYMETAL",
    "NIFTYPSUBANK",
    "NIFTYPVTBANK",
    # Issue #241 missing-on-2026-06-30 set (the 11 sectoral indices) —
    # supersets the 8 above (NIFTYAUTO/FMCG/IT/METAL/PSUBANK/PVTBANK are
    # already there; this adds the remaining 5).
    "NIFTYREALTY",
    "NIFTYPHARMA",
    "NIFTYOILANDGAS",
    "NIFTYCONSUMPTION",
    "NIFTYCONSRDURBL",
)


def _verify_canonical_nse_index_symbols(broker: str) -> list[str]:
    """Return the list of canonical NSE_INDEX symbols that did NOT resolve to a
    token in the freshly-loaded cache.

    Empty list means everything is fine. A non-empty list is logged as a
    WARNING by :func:`load_symbols_to_cache` so a missing-index regression
    surfaces on the next restart instead of biting downstream services
    (scanner_presubscribe / ws_recovery / sector_follow) at the first
    intraday tick.

    Read-only: a token miss here is observational; no DB writes happen.
    """
    try:
        from database.token_db_enhanced import get_token
    except Exception:
        # Import-time failure of token_db_enhanced is its own incident —
        # don't compound it with a hard error from a verification hook.
        logger.exception(
            "post-sync verification: failed to import get_token — "
            "skipping NSE_INDEX symbol verification (broker=%s)",
            broker,
        )
        return []

    missing: list[str] = []
    for sym in _REQUIRED_NSE_INDEX_SYMBOLS:
        try:
            tok = get_token(sym, "NSE_INDEX")
        except Exception:
            # A lookup that raises is just as much of a miss as one that
            # returns None — the downstream consumer would treat it the same.
            logger.exception(
                "post-sync verification: token lookup raised for %s on NSE_INDEX",
                sym,
            )
            tok = None
        if not tok:
            missing.append(sym)
    return missing


def load_symbols_to_cache(broker: str) -> bool:
    """
    Load all symbols into memory cache after master contract download
    This function is called automatically when master contract download completes

    Args:
        broker: The broker name for which symbols were downloaded

    Returns:
        bool: True if cache loaded successfully, False otherwise
    """
    try:
        logger.info(f"Starting cache load for broker: {broker}")
        start_time = time.time()

        # Import the enhanced token_db module
        from database.token_db_enhanced import get_cache_stats, load_cache_for_broker

        # Load all symbols into cache
        success = load_cache_for_broker(broker)

        if success:
            load_time = time.time() - start_time
            stats = get_cache_stats()

            logger.info(
                f"Successfully loaded {stats['total_symbols']} symbols into cache "
                f"in {load_time:.2f} seconds"
            )

            # Defence-in-depth (issue #241): immediately after cache load,
            # verify that every canonical NSE_INDEX symbol downstream
            # consumers depend on resolves to a non-empty token. A miss
            # surfaces as a single, easy-to-grep WARNING in the boot log so
            # the operator notices BEFORE the next 15:20 sector_follow cycle
            # silently degrades (the original 2026-06-30 incident).
            try:
                missing = _verify_canonical_nse_index_symbols(broker)
            except Exception:
                logger.exception(
                    "post-sync verification raised — continuing boot (broker=%s)",
                    broker,
                )
                missing = []
            if missing:
                logger.warning(
                    "post-sync verification: %d canonical NSE_INDEX symbol(s) "
                    "did NOT resolve after master-contract sync (broker=%s): %s. "
                    "Downstream consumers (scanner_presubscribe, ws_recovery, "
                    "sector_follow) will fail closed for these symbols.",
                    len(missing),
                    broker,
                    ", ".join(missing),
                )

            # Emit success event to frontend
            socketio.emit(
                "cache_loaded",
                {
                    "status": "success",
                    "broker": broker,
                    "total_symbols": stats["total_symbols"],
                    "memory_usage_mb": stats["stats"]["memory_usage_mb"],
                    "load_time": f"{load_time:.2f}",
                },
            )

            return True
        else:
            logger.error(f"Failed to load symbols into cache for broker: {broker}")

            # Emit error event to frontend
            socketio.emit(
                "cache_loaded",
                {
                    "status": "error",
                    "broker": broker,
                    "message": "Failed to load symbols into cache",
                },
            )

            return False

    except Exception as e:
        logger.exception(f"Error loading symbols to cache: {e}")

        # Emit error event to frontend
        socketio.emit("cache_loaded", {"status": "error", "broker": broker, "message": str(e)})

        return False


def hook_into_master_contract_download(broker: str):
    """
    Hook function to be called after master contract download completes
    This should be integrated into the existing master contract download flow

    Args:
        broker: The broker name for which master contract was downloaded
    """
    try:
        # Wait a moment for database transactions to complete
        time.sleep(0.5)

        # Load symbols into cache
        load_symbols_to_cache(broker)

        # After successful master contract download, restore Python strategies
        try:
            from blueprints.python_strategy import restore_strategies_after_login

            logger.info("Attempting to restore Python strategies after master contract download")
            success, message = restore_strategies_after_login()
            logger.info(f"Python strategy restoration result: {message}")
        except ImportError:
            logger.debug("Python strategy module not available")
        except Exception as strategy_error:
            logger.exception(f"Error restoring Python strategies: {strategy_error}")

    except Exception as e:
        logger.exception(f"Error in master contract cache hook: {e}")


def clear_cache_on_logout():
    """
    Clear the cache when user logs out or session expires
    This helps free memory and ensures fresh data on next login
    """
    try:
        from database.token_db_enhanced import clear_cache, get_cache_stats

        # Get stats before clearing
        stats = get_cache_stats()
        symbols_cleared = stats.get("total_symbols", 0)

        # Clear the cache
        clear_cache()

        logger.info(f"Cache cleared. Removed {symbols_cleared} symbols from memory")

    except Exception as e:
        logger.exception(f"Error clearing cache on logout: {e}")


def refresh_cache_if_needed(broker: str):
    """
    Check if cache needs refresh and reload if necessary
    Called periodically or on-demand

    Args:
        broker: The broker name to check cache for
    """
    try:
        from database.token_db_enhanced import get_cache

        cache = get_cache()

        # Check if cache is valid
        if not cache.is_cache_valid():
            logger.info(f"Cache expired or invalid for broker: {broker}. Reloading...")
            load_symbols_to_cache(broker)
        else:
            logger.debug(f"Cache is still valid for broker: {broker}")

    except Exception as e:
        logger.exception(f"Error checking cache validity: {e}")


def get_cache_health() -> dict:
    """
    Get cache health information for monitoring

    Returns:
        dict: Cache health metrics
    """
    try:
        from database.token_db_enhanced import get_cache_stats

        stats = get_cache_stats()

        # Calculate health score
        hit_rate = float(stats["stats"]["hit_rate"].rstrip("%"))
        cache_loaded = stats["cache_loaded"]
        cache_valid = stats["cache_valid"]
        total_queries = stats["stats"].get("hits", 0) + stats["stats"].get("misses", 0)

        health_score = 100
        if not cache_loaded:
            health_score = 0
        elif not cache_valid:
            health_score = 50
        elif total_queries > 10 and hit_rate < 90:
            # Only penalize hit rate if there have been enough queries to be meaningful
            health_score = 75

        return {
            "health_score": health_score,
            "status": "healthy"
            if health_score >= 75
            else "degraded"
            if health_score >= 50
            else "unhealthy",
            "cache_loaded": cache_loaded,
            "cache_valid": cache_valid,
            "hit_rate": stats["stats"]["hit_rate"],
            "total_symbols": stats["total_symbols"],
            "memory_usage_mb": stats["stats"]["memory_usage_mb"],
            "db_queries": stats["stats"]["db_queries"],
            "recommendations": _get_health_recommendations(health_score, stats),
        }

    except Exception as e:
        logger.exception(f"Error getting cache health: {e}")
        return {"health_score": 0, "status": "error", "error": str(e)}


def _get_health_recommendations(health_score: int, stats: dict) -> list:
    """
    Get recommendations based on cache health

    Args:
        health_score: Current health score
        stats: Cache statistics

    Returns:
        list: List of recommendation strings
    """
    recommendations = []

    if health_score == 0:
        recommendations.append("Cache is not loaded. Run master contract download.")
    elif health_score == 50:
        recommendations.append("Cache has expired. Login again or refresh master contract.")
    elif health_score == 75:
        hit_rate = float(stats["stats"]["hit_rate"].rstrip("%"))
        total_queries = stats["stats"].get("hits", 0) + stats["stats"].get("misses", 0)
        if total_queries > 10 and hit_rate < 90:
            recommendations.append(
                f"Cache hit rate is low ({hit_rate:.1f}%). Consider checking symbol mappings."
            )

    db_queries = stats["stats"].get("db_queries", 0)
    if db_queries > 100:
        recommendations.append(
            f"High number of DB queries ({db_queries}). Cache may not be working properly."
        )

    return recommendations if recommendations else ["Cache is operating optimally."]
