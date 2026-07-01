# Load and check environment variables before anything else
from utils.env_check import load_and_check_env_variables  # Import the environment check function

load_and_check_env_variables()

import os
import re
import sys

# Show loading indicator early (before heavy imports) so user sees immediate feedback.
# The full banner with "Ready" status prints later, right before the server accepts connections.
if __name__ == "__main__":
    _debug = os.getenv("FLASK_DEBUG", "False").lower() in ("true", "1", "t")
    _is_reloader_parent = _debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true"
    if not _is_reloader_parent:
        print("\033[93mStarting OpenAlgo...\033[0m", flush=True)

import mimetypes

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/json", ".json")
mimetypes.add_type("application/font-woff", ".woff")
mimetypes.add_type("application/font-woff2", ".woff2")

from flask import Flask, session
from flask_wtf.csrf import CSRFProtect  # Import CSRF protection

# Stage 1.5: importing the package triggers self-registration of every
# strategy declared under strategies/<name>/strategy.py. Today this is just
# ``trending_equity_intraday`` (a thin adapter around the legacy
# SimplifiedStockEngine — see strategies/__init__.py); future strategies
# will land here too. The simplified engine itself is still instantiated
# lazily via get_simplified_stock_engine_service(); this import only
# populates the registry so callers (the upcoming multi-strategy router,
# Stage 1.7's regime-profile reviewer) can enumerate available strategies.
import strategies  # noqa: F401
from blueprints.admin import admin_bp  # Import the admin blueprint
from blueprints.analyzer import analyzer_bp  # Import the analyzer blueprint
from blueprints.apikey import api_key_bp
from blueprints.auth import auth_bp
from blueprints.backtest import backtest_bp  # MVP backtester endpoints
from blueprints.brlogin import brlogin_bp
from blueprints.broker_credentials import (
    broker_credentials_bp,  # Import the broker credentials blueprint
)
from blueprints.chartink import chartink_bp  # Import the chartink blueprint
from blueprints.core import core_bp
from blueprints.custom_straddle import custom_straddle_bp  # Import custom straddle blueprint
from blueprints.dashboard import dashboard_bp
from blueprints.flow import flow_bp  # Import the flow blueprint
from blueprints.futures_follow import futures_follow_bp  # futures_follow_cap50 observability
from blueprints.gc_json import gc_json_bp
from blueprints.gex import gex_bp  # Import the GEX blueprint
from blueprints.health import health_bp  # Import the health monitoring blueprint
from blueprints.historify import historify_bp  # Import the historify blueprint
from blueprints.ivchart import ivchart_bp  # Import the IV chart blueprint
from blueprints.ivsmile import ivsmile_bp  # Import the IV Smile blueprint
from blueprints.journal import journal_bp  # Stage 2 trade journal inspection endpoints
from blueprints.latency import latency_bp  # Import the latency blueprint
from blueprints.leverage import leverage_bp  # Import the leverage blueprint
from blueprints.log import log_bp
from blueprints.logging import logging_bp  # Import the logging blueprint
from blueprints.master_contract_status import (
    master_contract_status_bp,  # Import the master contract status blueprint
)
from blueprints.mode_status import mode_status_bp  # Stage-0 mode resolver status endpoint
from blueprints.oiprofile import oiprofile_bp  # Import the OI Profile blueprint
from blueprints.oitracker import oitracker_bp  # Import the OI tracker blueprint
from blueprints.orders import orders_bp
from blueprints.platforms import platforms_bp
from blueprints.playground import playground_bp  # Import the API playground blueprint
from blueprints.pnltracker import pnltracker_bp  # Import the pnl tracker blueprint
from blueprints.preflight import preflight_bp  # Stage-0 go/no-go preflight gate
from blueprints.python_strategy import initialize_with_app_context as init_python_strategy
from blueprints.python_strategy import python_strategy_bp  # Import the python strategy blueprint
from blueprints.react_app import (  # Import React frontend blueprint
    is_react_frontend_available,
    react_bp,
    serve_react_app,
)
from blueprints.sandbox import sandbox_bp  # Import the sandbox blueprint
from blueprints.scanner_api import scanner_api_bp  # In-house scanner browser API (Tier 1)
from blueprints.search import search_bp
from blueprints.sector_follow import sector_follow_bp  # sector_follow_cap5_vol observability
from blueprints.security import security_bp  # Import the security blueprint
from blueprints.settings import settings_bp  # Import the settings blueprint
from blueprints.straddle_chart import straddle_bp  # Import the straddle chart blueprint
from blueprints.strategies_dashboard_api import (
    strategies_dashboard_bp,  # Strategies Dashboard API (Tier 2)
)
from blueprints.strategy import strategy_bp  # Import the strategy blueprint
from blueprints.strategy_chart import strategy_chart_bp  # Import the strategy chart blueprint
from blueprints.strategy_portfolio import strategy_portfolio_bp  # Strategy Builder portfolio
from blueprints.system_permissions import (
    system_permissions_bp,  # Import the system permissions blueprint
)
from blueprints.telegram import telegram_bp  # Import the telegram blueprint
from blueprints.traffic import traffic_bp  # Import the traffic blueprint
from blueprints.tv_json import tv_json_bp
from blueprints.vol_surface import vol_surface_bp  # Import the vol surface blueprint
from blueprints.websocket_example import websocket_bp  # Import the websocket example blueprint
from blueprints.whatsapp import whatsapp_bp  # Import the WhatsApp blueprint
from cors import cors  # Import the CORS instance
from csp import apply_csp_middleware  # Import the CSP middleware
from database.action_center_db import init_db as ensure_action_center_tables_exists
from database.analyzer_db import init_db as ensure_analyzer_tables_exists
from database.apilog_db import init_db as ensure_api_log_tables_exists
from database.auth_db import init_db as ensure_auth_tables_exists
from database.backtest_db import init_db as ensure_backtest_tables_exists
from database.chartink_db import init_db as ensure_chartink_tables_exists
from database.daily_intent_db import init_db as ensure_daily_intent_tables_exists
from database.data_health_db import init_db as ensure_data_health_tables_exists
from database.flow_db import init_db as ensure_flow_tables_exists
from database.futures_follow_db import init_db as ensure_futures_follow_tables_exists
from database.historify_db import init_database as ensure_historify_tables_exists
from database.journal_reflection_db import init_db as ensure_journal_reflection_tables_exists
from database.latency_db import init_latency_db as ensure_latency_tables_exists
from database.leverage_db import init_db as ensure_leverage_tables_exists
from database.sandbox_db import init_db as ensure_sandbox_tables_exists
from database.scan_cycle_db import init_db as ensure_scan_cycle_tables_exists
from database.scanner_comparison_db import (
    init_db as ensure_scanner_comparison_tables_exists,
)
from database.scanner_db import init_db as ensure_scanner_tables_exists
from database.settings_db import init_db as ensure_settings_tables_exists
from database.signal_decision_db import init_db as ensure_signal_decision_tables_exists
from database.strategy_daily_intent_db import init_db as ensure_strategy_daily_intent_tables_exists
from database.strategy_db import init_db as ensure_strategy_tables_exists
from database.symbol import init_db as ensure_master_contract_tables_exists
from database.telegram_db import get_bot_config
from database.trade_journal_db import init_db as ensure_trade_journal_tables_exists
from database.traffic_db import init_logs_db as ensure_traffic_logs_exists
from database.user_db import init_db as ensure_user_tables_exists
from database.whatsapp_db import (
    get_bot_config as get_whatsapp_bot_config,  # noqa: F401  (triggers module-level init_db)
)
from extensions import socketio  # Import SocketIO
from limiter import limiter  # Import the Limiter instance
from restx_api import api, api_v1_bp
from services.telegram_bot_service import telegram_bot_service
from services.thread_watchdog_service import init_thread_watchdog  # Thread-count watchdog
from utils.health_monitor import init_health_monitoring  # Import health monitoring
from utils.latency_monitor import init_latency_monitoring  # Import latency monitoring
from utils.logging import (  # Import centralized logging
    get_logger,
    highlight_url,
    log_startup_banner,
)
from utils.plugin_loader import load_broker_auth_functions, load_broker_capabilities
from utils.security_middleware import init_security_middleware  # Import security middleware
from utils.socketio_error_handler import (
    init_socketio_error_handling,  # Import Socket.IO error handler
)
from utils.traffic_logger import init_traffic_logging  # Import traffic logging
from utils.version import get_version  # Import version management

# Import WebSocket proxy server - using relative import to avoid @ symbol issues
from websocket_proxy.app_integration import start_websocket_proxy

# Initialize logger
logger = get_logger(__name__)


def create_app(testing: bool = False):
    # Initialize Flask application
    app = Flask(__name__)
    app.config["TESTING"] = testing

    # Initialize SocketIO
    socketio.init_app(app)  # Link SocketIO to the Flask app

    # Initialize EventBus subscribers
    from subscribers import register_all as register_event_subscribers

    register_event_subscribers()

    # Initialize CSRF protection
    csrf = CSRFProtect(app)

    # Store csrf instance in app config for use in other modules
    app.csrf = csrf

    # Initialize Flask-Limiter with the app object
    limiter.init_app(app)

    # Initialize Flask-CORS with the app object using configuration from environment variables
    from cors import get_cors_config

    cors.init_app(app, **get_cors_config())

    # Apply Content Security Policy middleware
    apply_csp_middleware(app)

    # Initialize Socket.IO error handling
    init_socketio_error_handling(socketio)

    # Register custom Jinja2 filters
    from utils.number_formatter import format_indian_number

    app.jinja_env.filters["indian_number"] = format_indian_number

    # Environment variables
    app.secret_key = os.getenv("APP_KEY")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL")

    # Dynamic cookie security configuration based on HOST_SERVER
    HOST_SERVER = os.getenv("HOST_SERVER", "http://127.0.0.1:5000")
    USE_HTTPS = HOST_SERVER.startswith("https://")

    # Configure session cookie security
    session_cookie_name = os.getenv("SESSION_COOKIE_NAME", "session")
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=USE_HTTPS,
        SESSION_COOKIE_NAME=session_cookie_name,
        # PERMANENT_SESSION_LIFETIME is dynamically set at login to expire at 3:30 AM IST
    )

    # Add cookie prefix for HTTPS environments
    if USE_HTTPS:
        app.config["SESSION_COOKIE_NAME"] = f"__Secure-{session_cookie_name}"

    # CSRF configuration from environment variables
    csrf_enabled = os.getenv("CSRF_ENABLED", "TRUE").upper() == "TRUE"
    app.config["WTF_CSRF_ENABLED"] = csrf_enabled

    # Configure CSRF cookie security to match session cookie
    csrf_cookie_name = os.getenv("CSRF_COOKIE_NAME", "csrf_token")
    app.config.update(
        WTF_CSRF_COOKIE_HTTPONLY=True,
        WTF_CSRF_COOKIE_SAMESITE="Lax",
        WTF_CSRF_COOKIE_SECURE=USE_HTTPS,
        WTF_CSRF_COOKIE_NAME=csrf_cookie_name,
    )

    # Add cookie prefix for CSRF token in HTTPS environments
    if USE_HTTPS:
        app.config["WTF_CSRF_COOKIE_NAME"] = f"__Secure-{csrf_cookie_name}"

    # Parse CSRF time limit from environment
    csrf_time_limit = os.getenv("CSRF_TIME_LIMIT", "").strip()
    if csrf_time_limit:
        try:
            app.config["WTF_CSRF_TIME_LIMIT"] = int(csrf_time_limit)
        except ValueError:
            app.config["WTF_CSRF_TIME_LIMIT"] = None  # Default to no limit if invalid
    else:
        app.config["WTF_CSRF_TIME_LIMIT"] = None  # No time limit if empty

    # Register RESTx API blueprint first
    # Register React frontend blueprint FIRST for migrated routes
    # Register React frontend routes
    if is_react_frontend_available():
        app.register_blueprint(react_bp)
        logger.debug("React frontend enabled (frontend/dist found)")
    else:
        logger.warning("React frontend not available - run 'npm run build' in frontend/")

    app.register_blueprint(api_v1_bp)

    # Exempt API endpoints from CSRF protection (they use API key authentication)
    csrf.exempt(api_v1_bp)

    # Initialize security middleware before traffic logging
    init_security_middleware(app)

    # Initialize traffic logging middleware after security
    init_traffic_logging(app)

    # Register other blueprints
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(orders_bp)
    app.register_blueprint(search_bp)
    app.register_blueprint(api_key_bp)
    app.register_blueprint(log_bp)
    app.register_blueprint(tv_json_bp)
    app.register_blueprint(gc_json_bp)
    app.register_blueprint(platforms_bp)
    app.register_blueprint(brlogin_bp)
    app.register_blueprint(core_bp)
    app.register_blueprint(analyzer_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(chartink_bp)
    app.register_blueprint(sector_follow_bp)  # sector_follow_cap5_vol observability/control
    app.register_blueprint(futures_follow_bp)  # futures_follow_cap50 observability/control
    app.register_blueprint(mode_status_bp)  # Stage-0 mode resolver status endpoint
    app.register_blueprint(preflight_bp)  # Stage-0 go/no-go preflight gate
    app.register_blueprint(journal_bp)  # Stage 2 trade journal inspection endpoints
    app.register_blueprint(backtest_bp)  # MVP backtester trigger + inspection endpoints
    app.register_blueprint(traffic_bp)
    app.register_blueprint(latency_bp)
    app.register_blueprint(leverage_bp)  # Register Leverage blueprint
    app.register_blueprint(health_bp)  # Register Health monitoring blueprint
    app.register_blueprint(strategy_bp)
    app.register_blueprint(master_contract_status_bp)
    app.register_blueprint(websocket_bp)  # Register WebSocket example blueprint
    app.register_blueprint(pnltracker_bp)  # Register PnL tracker blueprint
    app.register_blueprint(python_strategy_bp)  # Register Python strategy blueprint
    app.register_blueprint(telegram_bp)  # Register Telegram blueprint
    app.register_blueprint(whatsapp_bp)  # Register WhatsApp blueprint
    app.register_blueprint(security_bp)  # Register Security blueprint
    app.register_blueprint(sandbox_bp)  # Register Sandbox blueprint
    app.register_blueprint(scanner_api_bp)  # Register in-house scanner browser API
    app.register_blueprint(strategies_dashboard_bp)  # Register Strategies Dashboard API (Tier 2)
    app.register_blueprint(playground_bp)  # Register API playground blueprint
    app.register_blueprint(logging_bp)  # Register Logging blueprint
    app.register_blueprint(admin_bp)  # Register Admin blueprint
    app.register_blueprint(historify_bp)  # Register Historify blueprint
    app.register_blueprint(ivchart_bp)  # Register IV chart blueprint
    app.register_blueprint(oitracker_bp)  # Register OI tracker blueprint
    app.register_blueprint(straddle_bp)  # Register straddle chart blueprint
    app.register_blueprint(strategy_chart_bp)  # Register strategy chart blueprint
    app.register_blueprint(custom_straddle_bp)  # Register custom straddle blueprint
    app.register_blueprint(vol_surface_bp)  # Register vol surface blueprint
    app.register_blueprint(gex_bp)  # Register GEX blueprint
    app.register_blueprint(ivsmile_bp)  # Register IV Smile blueprint
    app.register_blueprint(oiprofile_bp)  # Register OI Profile blueprint
    app.register_blueprint(flow_bp)  # Register Flow blueprint
    app.register_blueprint(broker_credentials_bp)  # Register Broker credentials blueprint
    app.register_blueprint(system_permissions_bp)  # Register System permissions blueprint
    app.register_blueprint(strategy_portfolio_bp)  # Register Strategy Portfolio blueprint

    # Remote MCP (HTTP + OAuth) — opt-in via MCP_HTTP_ENABLED. Off by default.
    # Pre-flight refusal: must NEVER coexist with FLASK_DEBUG=True (debug-mode
    # tracebacks would leak bearer tokens). See docs/prd/remote-mcp.md.
    if os.getenv("MCP_HTTP_ENABLED", "False").lower() == "true":
        # Match Flask's own truthy parsing (Flask accepts "1"/"t"/"true").
        # The narrow `== "true"` check we used to do let FLASK_DEBUG=1
        # slip past this guard while still putting Flask in debug mode.
        if os.getenv("FLASK_DEBUG", "False").lower() in ("true", "1", "t"):
            raise RuntimeError(
                "MCP_HTTP_ENABLED=True is not allowed with FLASK_DEBUG enabled. "
                "Debug-mode tracebacks leak bearer tokens. Disable one of them."
            )

        # Hard requirement: MCP_PUBLIC_URL anchors the JWT iss/aud claims.
        # Without it, tokens issued by two unconfigured instances would
        # validate against each other (security review finding H-1).
        if not os.getenv("MCP_PUBLIC_URL"):
            raise RuntimeError(
                "MCP_HTTP_ENABLED=True requires MCP_PUBLIC_URL to be set to "
                "the canonical HTTPS origin (e.g. https://mcp.yourdomain.com). "
                "Without it, JWT iss/aud claims collapse to empty strings and "
                "tokens become portable across instances."
            )

        # Crucial ordering: set OPENALGO_MCP_HTTP_BOOT BEFORE importing the
        # MCP HTTP blueprint. The blueprint transitively imports
        # mcp.mcpserver, which checks this env var to skip the stdio
        # argv requirement. Stdio launches never set this var, so their
        # behavior is unaffected.
        os.environ["OPENALGO_MCP_HTTP_BOOT"] = "1"

        from blueprints.mcp_http import mcp_http_bp
        from blueprints.mcp_oauth import mcp_oauth_bp, mcp_wellknown_bp
        from database.oauth_db import init_db as init_oauth_db
        from utils.oauth_keys import ensure_signing_key

        # Idempotent: tables created if missing, signing key generated on
        # first run. Ordering matters — ensure_signing_key writes a row
        # to oauth_signing_keys, so the table must exist first.
        init_oauth_db()
        ensure_signing_key()

        app.register_blueprint(mcp_oauth_bp)
        app.register_blueprint(mcp_wellknown_bp)
        app.register_blueprint(mcp_http_bp)

        # Externally-facing OAuth endpoints and the MCP transport are
        # called by hosted clients (claude.ai etc.) that have NO
        # OpenAlgo session cookie. Flask-WTF's global CSRFProtect would
        # 400 every request without these exemptions (security review
        # finding C-1). Authentication on these endpoints is via
        # Bearer token (transport) or client_secret + PKCE (token /
        # revoke) — CSRF cookie protection doesn't apply.
        # /oauth/authorize POST is intentionally NOT exempted: it's
        # browser-driven from the OpenAlgo session and uses the
        # rendered consent form's csrf_token field.
        with app.app_context():
            for endpoint in (
                "mcp_oauth_bp.token_endpoint",
                "mcp_oauth_bp.revoke_endpoint",
                "mcp_oauth_bp.register_client",
                "mcp_http_bp.mcp_dispatch",
                "mcp_http_bp.mcp_sse",
            ):
                view = app.view_functions.get(endpoint)
                if view is not None:
                    csrf.exempt(view)

        # Boot warnings for non-default security postures so an admin
        # who flipped these months ago and forgot is reminded on every
        # restart (security review finding L-3).
        if os.getenv("MCP_OAUTH_WRITE_SCOPE_ENABLED", "True").lower() == "true":
            logger.warning(
                "[MCP] write:orders scope is ENABLED — MCP clients can place real orders."
            )
        if os.getenv("MCP_OAUTH_REQUIRE_APPROVAL", "False").lower() != "true":
            logger.warning(
                "[MCP] DCR auto-approval is ENABLED — any DCR registration "
                "can immediately complete OAuth without admin review."
            )

        logger.info("Remote MCP blueprints registered (OAuth + JSON-RPC dispatch + SSE).")

    # Exempt webhook endpoints from CSRF protection after app initialization
    with app.app_context():
        # Exempt webhook endpoints from CSRF protection
        csrf.exempt(app.view_functions["chartink_bp.webhook"])
        csrf.exempt(app.view_functions["chartink_bp.simplified_stock_engine_webhook"])
        csrf.exempt(app.view_functions["chartink_bp.record_aborted_cycle_endpoint"])
        csrf.exempt(app.view_functions["strategy_bp.webhook"])
        csrf.exempt(app.view_functions["flow.trigger_webhook"])
        csrf.exempt(app.view_functions["flow.trigger_webhook_with_symbol"])

        # Exempt broker callback endpoints from CSRF protection (OAuth callbacks from external providers)
        csrf.exempt(app.view_functions["brlogin.broker_callback"])

        # Exempt Samco 2FA setup endpoints from CSRF (JSON API calls from React frontend)
        csrf.exempt(app.view_functions["brlogin.samco_generate_otp"])
        csrf.exempt(app.view_functions["brlogin.samco_generate_secret"])
        csrf.exempt(app.view_functions["brlogin.samco_save_secret"])
        csrf.exempt(app.view_functions["brlogin.samco_ip_status"])
        csrf.exempt(app.view_functions["brlogin.samco_update_ip"])

        # Exempt logout endpoint from CSRF protection (safe - only destroys session)
        csrf.exempt(app.view_functions["auth.logout"])

        # Exempt health check endpoints from CSRF (for AWS ELB, K8s probes)
        csrf.exempt(app.view_functions["health_bp.simple_health"])
        csrf.exempt(app.view_functions["health_bp.detailed_health_check"])

        if not testing:
            # Initialize latency monitoring (after registering API blueprint)
            init_latency_monitoring(app)

            # Initialize health monitoring (background daemon thread)
            init_health_monitoring(app)

            # Initialize thread-count watchdog (WARN>100, CRIT>200, Telegram via anomaly_alert)
            init_thread_watchdog(app)

        # NOTE: Python strategy scheduler is initialized in setup_environment()
        # AFTER database tables are created, to avoid "no such table" errors on fresh install

        # NOTE: Telegram bot auto-start moved to background init thread
        # (after DB tables are created) to avoid "no such table" on fresh install

    @app.before_request
    def wait_for_db_ready():
        """Block requests until background database initialization completes."""
        from flask import request

        # Static assets don't need DB
        if request.path.startswith("/static/") or request.path.startswith("/assets/"):
            return

        # Wait up to 30s for DB init (typically ~3.5s)
        if hasattr(app, "db_ready") and not app.db_ready.is_set():
            app.db_ready.wait(timeout=30)

    @app.before_request
    def check_session_expiry():
        """Check session validity before each request"""
        from flask import request

        from utils.session import is_session_valid, revoke_user_tokens

        # Skip session check for static files, API endpoints, and public routes
        if (
            request.path.startswith("/static/")
            or request.path.startswith("/api/")
            or request.path.startswith("/assets/")  # React frontend assets
            or request.path
            in [
                "/",
                "/auth/login",
                "/auth/reset-password",
                "/auth/csrf-token",
                "/auth/broker-config",
                "/auth/session-status",  # Session status check for React SPA
                "/auth/check-setup",  # Setup check for React SPA
                "/setup",
                "/download",
                "/faq",
                "/login",  # React login page
            ]
            or request.path.startswith("/auth/broker/")  # OAuth callbacks
            or request.path.startswith("/_reload-ws")
        ):  # WebSocket reload endpoint
            return

        # Check if user is logged in and session is expired
        if session.get("logged_in") and not is_session_valid():
            logger.info(f"Session expired for user: {session.get('user')} - revoking tokens")
            revoke_user_tokens(revoke_db_tokens=False)
            session.clear()
            # Don't redirect here, let individual routes handle it

    @app.errorhandler(400)
    def csrf_error(error):
        """Custom handler for CSRF errors (400 Bad Request)"""
        from flask import flash, jsonify, redirect, request, url_for

        error_description = str(error)

        logger.warning(f"CSRF Error on {request.path}: {error_description}")

        # Check if it's a CSRF error
        if "CSRF" in error_description or "csrf" in error_description.lower():
            if request.is_json or request.path.startswith("/api"):
                return jsonify(
                    {
                        "error": "CSRF validation failed",
                        "message": "Security token expired or invalid. Please refresh the page and try again.",
                    }
                ), 400
            else:
                flash("Security token expired. Please try again.", "error")
                return redirect(request.referrer or url_for("auth.login"))

        # For other 400 errors
        return str(error), 400

    @app.errorhandler(404)
    def not_found_error(error):
        from flask import request, session

        from database.traffic_db import Error404Tracker
        from utils.ip_helper import get_real_ip

        client_ip = get_real_ip()
        path = request.path

        # Skip 404 tracking for authenticated users (prevents self-ban during
        # login flows, broker OAuth callbacks, or normal navigation to
        # React routes that don't have explicit Flask endpoints)
        is_authenticated = session.get("logged_in", False)

        # Skip tracking for common browser/crawler requests that are not attack probes
        safe_prefixes = (
            "/favicon",
            "/robots.txt",
            "/sitemap",
            "/manifest",
            "/sw.js",
            "/.well-known",
            "/apple-touch-icon",
            "/service-worker",
            "/workbox",
        )

        if not is_authenticated and not path.startswith(safe_prefixes):
            Error404Tracker.track_404(client_ip, path)

        # Serve React app (React Router handles 404)
        return serve_react_app()

    @app.errorhandler(500)
    def internal_server_error(e):
        """Custom handler for 500 Internal Server Error"""
        from flask import redirect

        # Log the error
        logger.error(f"Server Error: {e}")

        # Redirect to React error page
        return redirect("/error")

    @app.errorhandler(429)
    def rate_limit_exceeded(e):
        """Custom handler for 429 Too Many Requests"""
        from flask import redirect, request

        # Log rate limit hit
        logger.warning(f"Rate limit exceeded for {request.remote_addr}: {request.path}")

        # For API requests, return JSON response
        if request.path.startswith("/api/"):
            return {
                "status": "error",
                "message": "Rate limit exceeded. Please slow down your requests.",
                "retry_after": 60,
            }, 429

        # For web requests, redirect to React rate-limited page
        return redirect("/rate-limited")

    @app.context_processor
    def inject_version():
        return {"version": get_version()}

    @app.route("/api/config/host")
    def get_host_config():
        """Return the HOST_SERVER configuration for frontend webhook URL generation"""
        from flask import jsonify

        host_server = os.getenv("HOST_SERVER", "http://127.0.0.1:5000")

        # Determine if webhook URL is externally accessible
        is_localhost = any(
            local in host_server.lower()
            for local in ["localhost", "127.0.0.1", "0.0.0.0"]  # nosec B104
        )

        return jsonify({"host_server": host_server, "is_localhost": is_localhost})

    @app.teardown_appcontext
    def shutdown_database_sessions(exception=None):
        """Remove all scoped sessions after each request to prevent FD leaks"""
        _sessions = [
            ("database.auth_db", "db_session"),
            ("database.traffic_db", "logs_session"),
            ("database.apilog_db", "db_session"),
            ("database.latency_db", "latency_session"),
            ("database.health_db", "health_session"),
            ("database.settings_db", "db_session"),
            ("database.strategy_db", "db_session"),
            ("database.user_db", "db_session"),
            ("database.action_center_db", "db_session"),
            ("database.qty_freeze_db", "db_session"),
            ("database.sandbox_db", "db_session"),
            ("database.analyzer_db", "db_session"),
            ("database.chart_prefs_db", "db_session"),
            ("database.chartink_db", "db_session"),
            ("database.flow_db", "db_session"),
            ("database.leverage_db", "db_session"),
            ("database.strategy_portfolio_db", "db_session"),
            ("database.market_calendar_db", "db_session"),
            ("database.telegram_db", "db_session"),
            ("database.symbol", "db_session"),
        ]
        for module_name, session_attr in _sessions:
            try:
                import importlib

                mod = importlib.import_module(module_name)
                session = getattr(mod, session_attr, None)
                if session is not None:
                    session.remove()
            except Exception:
                pass

    return app


def setup_environment(app):
    with app.app_context():
        # load broker plugins (lazy - no actual imports until login)
        app.broker_auth_functions = load_broker_auth_functions()
        load_broker_capabilities()  # cache plugin.json data in memory

    # Setup ngrok cleanup handlers (always register, regardless of ngrok being enabled)
    # This ensures proper cleanup on shutdown even if ngrok is enabled/disabled via UI
    # The actual tunnel creation happens in the __main__ block below
    from utils.ngrok_manager import setup_ngrok_handlers

    setup_ngrok_handlers()

    # Run database init + schedulers in background thread
    # Tables already exist after first run; this is a safety check
    import threading

    # Event to signal when DB init is complete (cache restoration waits on this)
    app.db_ready = threading.Event()

    def _init_databases_and_schedulers():
        with app.app_context():
            import time
            from concurrent.futures import ThreadPoolExecutor, as_completed

            from database.chart_prefs_db import ensure_chart_prefs_tables_exists
            from database.market_calendar_db import ensure_market_calendar_tables_exists
            from database.qty_freeze_db import ensure_qty_freeze_tables_exists
            from database.strategy_portfolio_db import (
                ensure_strategy_portfolio_tables_exists,
            )

            db_init_functions = [
                ("Auth DB", ensure_auth_tables_exists),
                ("User DB", ensure_user_tables_exists),
                ("Master Contract DB", ensure_master_contract_tables_exists),
                ("API Log DB", ensure_api_log_tables_exists),
                ("Analyzer DB", ensure_analyzer_tables_exists),
                ("Settings DB", ensure_settings_tables_exists),
                ("Daily Intent DB", ensure_daily_intent_tables_exists),
                ("Strategy Daily Intent DB", ensure_strategy_daily_intent_tables_exists),
                ("Data Health DB", ensure_data_health_tables_exists),
                ("Scan Cycle DB", ensure_scan_cycle_tables_exists),
                ("Scanner DB", ensure_scanner_tables_exists),
                ("Scanner Comparison DB", ensure_scanner_comparison_tables_exists),
                ("Signal Decision DB", ensure_signal_decision_tables_exists),
                ("Trade Journal DB", ensure_trade_journal_tables_exists),
                ("Journal Reflection DB", ensure_journal_reflection_tables_exists),
                ("Backtest DB", ensure_backtest_tables_exists),
                ("Chartink DB", ensure_chartink_tables_exists),
                ("Traffic Logs DB", ensure_traffic_logs_exists),
                ("Latency DB", ensure_latency_tables_exists),
                ("Strategy DB", ensure_strategy_tables_exists),
                ("Sandbox DB", ensure_sandbox_tables_exists),
                ("Action Center DB", ensure_action_center_tables_exists),
                ("Chart Prefs DB", ensure_chart_prefs_tables_exists),
                ("Market Calendar DB", ensure_market_calendar_tables_exists),
                ("Qty Freeze DB", ensure_qty_freeze_tables_exists),
                ("Historify DB", ensure_historify_tables_exists),
                ("Flow DB", ensure_flow_tables_exists),
                ("Futures Follow DB", ensure_futures_follow_tables_exists),
                ("Leverage DB", ensure_leverage_tables_exists),
                ("Strategy Portfolio DB", ensure_strategy_portfolio_tables_exists),
            ]

            db_init_start = time.time()
            with ThreadPoolExecutor(max_workers=15) as executor:
                futures = {executor.submit(func): name for name, func in db_init_functions}
                for future in as_completed(futures):
                    db_name = futures[future]
                    try:
                        future.result()
                    except Exception as e:
                        logger.error(f"Failed to initialize {db_name}: {e}")

            db_init_time = (time.time() - db_init_start) * 1000
            logger.debug(f"All databases initialized in parallel ({db_init_time:.0f}ms)")

            # Backfill the legacy daily_intent table into the unified
            # strategy_daily_intent table (idempotent; runs after both tables
            # exist). See docs/design/strategy_daily_intent.md.
            try:
                from database.strategy_daily_intent_db import migrate_legacy_daily_intent

                migrate_legacy_daily_intent()
            except Exception as e:
                logger.error(f"strategy_daily_intent migration skipped: {e}")

            # Mode-only architecture (2026-06-12): ensure the persistent
            # strategy_mode knob and the ephemeral strategy_runtime_override
            # safety-guard table exist on every boot. Without this, the engines'
            # runtime-override reads and the safety guards' override writes would
            # hit a missing table (and silently fail-open / fail-safe).
            try:
                from database.strategy_llm_config_db import (
                    init_db as _init_strategy_llm_config,
                )
                from database.strategy_mode_db import init_db as _init_strategy_mode
                from database.strategy_runtime_override_db import (
                    init_db as _init_strategy_runtime_override,
                )

                _init_strategy_mode()
                _init_strategy_runtime_override()
                _init_strategy_llm_config()
            except Exception as e:
                logger.error(f"strategy_mode/runtime_override table init skipped: {e}")

            # Signal that DB tables are ready (unblocks cache restoration)
            app.db_ready.set()

            # Initialize schedulers AFTER database initialization
            try:
                init_python_strategy()
                logger.debug("Python strategy scheduler initialized")
            except Exception as e:
                logger.error(f"Failed to initialize Python strategy scheduler: {e}")

            try:
                from services.flow_scheduler_service import init_flow_scheduler

                init_flow_scheduler()
                logger.debug("Flow scheduler initialized")
            except Exception as e:
                logger.error(f"Failed to initialize Flow scheduler: {e}")

            try:
                from services.historify_scheduler_service import init_historify_scheduler

                init_historify_scheduler(socketio=socketio)
                logger.debug("Historify scheduler initialized")
            except Exception as e:
                logger.error(f"Failed to initialize Historify scheduler: {e}")

            # Probe historify.duckdb for orphan-process contention BEFORE wiring
            # any backfill scheduler. If a foreign python.exe is holding the
            # file (a prior OpenAlgo that didn't fully die, a stuck backtester),
            # the four boot backfill jobs would otherwise pile 200+ lock-error
            # retries into errors.jsonl against a file they can never write to.
            # Aborts boot loud with the holder PID. See services/boot_db_probe.py
            # and issue #139.
            try:
                from services.boot_db_probe import assert_historify_unlocked

                assert_historify_unlocked()
            except SystemExit:
                raise  # propagate the abort — the probe already logged + alerted
            except Exception as e:
                logger.error(f"boot_db_probe wiring failed (non-fatal): {e}")

            # sector_follow_cap5_vol 1m feed convergence (replaces the 16:05/16:10
            # IST cron backfill jobs). On boot — after a broker session appears —
            # it reads MAX(timestamp) per index + stock from historify.duckdb and
            # catches up only the stale tail, then a periodic loop re-checks during
            # the post-close publish window (15:30..17:00 IST). Non-blocking: the
            # boot check runs on a daemon thread. See
            # services/sector_follow_backfill_scheduler.py.
            try:
                from services.sector_follow_backfill_scheduler import (
                    init_sector_follow_backfill,
                )

                init_sector_follow_backfill(app=app)
                logger.debug("Sector Follow backfill convergence initialized")
            except Exception as e:
                logger.error(f"Failed to initialize Sector Follow backfill convergence: {e}")

            # In-house scanner SCANNER_SYMBOLS universe feed convergence (both 1m
            # AND daily). The scanner-side sibling of the sector_follow service
            # above — it closes the two supply bugs the 2026-06-13 Friday replay
            # surfaced (the scanner universe was never backfilled; the stored-D
            # interval was universally stale). On boot — after a broker session
            # appears — it reads MAX(timestamp) per symbol for each interval from
            # historify.duckdb and catches up only the stale tail, then a periodic
            # loop re-checks during the post-close publish window (15:30..17:00
            # IST). Non-blocking daemon thread. See
            # services/scanner_backfill_scheduler.py.
            try:
                from services.scanner_backfill_scheduler import (
                    init_scanner_backfill_scheduler,
                )

                init_scanner_backfill_scheduler(app=app)
                logger.debug("Scanner backfill convergence initialized")
            except Exception as e:
                logger.error(f"Failed to initialize Scanner backfill convergence: {e}")

            # Sector Follow CAP5_VOL strategy (R40 deployable variant). Default
            # mode=scaffold means loading this changes ZERO live trading behavior
            # — it only registers 15:20/15:25/09:00 IST jobs that compute + log.
            # See strategies/sector_follow_cap5_vol/ and SECTOR_FOLLOW_CAP5_VOL_MODE.
            try:
                from services.sector_follow_service import init_sector_follow_service

                init_sector_follow_service(app=app)
                logger.debug("Sector Follow CAP5_VOL service initialized")
            except Exception as e:
                logger.error(f"Failed to initialize Sector Follow service: {e}")

            # In-house scanner pre-entry smoke check (Tier 2, 09:18 IST). Closes
            # the gap CLAUDE.md flags in the Tier-1 hardening section: a total
            # feed outage produces no bar closes, so the per-cycle completeness
            # metric never fires. The smoke check is the upstream gate.
            # Read-only on DBs + scanner state; failure path is Telegram CRIT
            # + data_health_check row. No runtime override is written (the
            # scanner is a passive 5m-bar consumer with no entry-job to gate;
            # visibility IS the fix). See
            # services/scanner_smoke_check_service.py and issue #32.
            try:
                from services.scanner_smoke_check_service import (
                    init_scanner_smoke_check,
                )

                init_scanner_smoke_check(app=app)
                logger.debug("Scanner smoke check initialized")
            except Exception as e:
                logger.error(f"Failed to initialize scanner smoke check: {e}")

            # In-house scanner zero-results tripwire (issue #33). Downstream
            # silent-failure detector that catches the Friday 2026-06-19 gap
            # the per-cycle completeness metric missed (56% coverage but 0 BUY
            # hits all day because daily gates ran against stale stored bars).
            # Fires every SCANNER_DRY_CHECK_INTERVAL_MIN minutes during market
            # hours; CRIT when Chartink is producing rows but in-house is
            # silent (pipeline degraded), WARN when both are dry (quiet
            # market). See services/scanner_dry_tripwire_service.py.
            try:
                from services.scanner_dry_tripwire_service import (
                    init_scanner_dry_tripwire,
                )

                init_scanner_dry_tripwire(app=app)
                logger.debug("Scanner dry tripwire initialized")
            except Exception as e:
                logger.error(f"Failed to initialize scanner dry tripwire: {e}")

            # Futures Follow CAP50 strategy (leveraged-beta NIFTY-futures sleeve on
            # the sector_follow signal set). Default mode=sandbox means it ACTIVELY
            # trades the virtual ₹1Cr sandbox book from boot — the
            # 09:00/15:14/15:20/15:25/15:30 IST jobs place real sandbox orders (no
            # live broker orders until the operator sets mode=live). See
            # strategies/futures_follow_cap50/ and FUTURES_FOLLOW_MODE.
            try:
                from services.futures_follow_service import init_futures_follow_service

                init_futures_follow_service(app=app)
                logger.debug("Futures Follow CAP50 service initialized")
            except Exception as e:
                logger.error(f"Failed to initialize Futures Follow service: {e}")

            # Scanner-vs-Chartink EOD comparison (retires the Cowork-side
            # "scanner-vs-chartink-daily-comparison" scheduled task). Registers a
            # single 15:45 IST mon-fri job that compares the in-house scanner's
            # BUY/SELL hits against the Chartink webhook lists, writes a
            # scanner_comparison row, and Telegrams the verdict. Read-only on every
            # DB except its own table; gated per-fire by SCANNER_COMPARISON_EOD_ENABLED.
            try:
                from services.scanner_comparison_eod_service import (
                    init_scanner_comparison_eod_service,
                )

                init_scanner_comparison_eod_service()
                logger.debug("Scanner comparison EOD job registered")
            except Exception as e:
                logger.error(f"Failed to register Scanner comparison EOD job: {e}")

            # Trading-day funnel (issue #159). Registers a single 15:35 IST
            # mon-fri job that walks the signal → engine → order → journal
            # pipeline, computes per-layer counts, and Telegrams the verdict
            # so the next "zero trades" day produces an immediate alert
            # naming the drop-off layer. Read-only on every DB; gated
            # per-fire by TRADING_DAY_FUNNEL_ENABLED.
            try:
                from services.trading_day_funnel_service import (
                    init_trading_day_funnel_service,
                )

                init_trading_day_funnel_service()
                logger.debug("Trading day funnel job registered")
            except Exception as e:
                logger.error(f"Failed to register Trading day funnel job: {e}")

            # Telegram INBOUND intent bot (Phase 6). Gated by
            # TELEGRAM_INBOUND_ENABLED (default false), so this is a no-op on
            # deploy until the operator flips the flag — it then polls Telegram
            # for /intent commands and writes strategy_daily_intent. Registers an
            # 08:45 IST morning-prompt job. See services/telegram_inbound_service.py.
            try:
                from services.telegram_inbound_service import init_telegram_inbound_service

                init_telegram_inbound_service(app=app)
            except Exception as e:
                logger.error(f"Failed to initialize Telegram inbound service: {e}")

            # Event-driven broker-WebSocket pre-subscribe wiring shared by the
            # scanner and the regime classifier. Three triggers (connect callback,
            # event-bus subscription, boot-retry thread) all converge on the
            # same idempotent ensure() — see services/scanner_presubscribe.py
            # ::wire_pre_subscribe for the full rationale.
            #
            # Issue #244 fix (2026-06-30): the api_key fetch now happens INSIDE
            # the boot-retry loop and a broker_session_refreshed event-bus
            # subscriber is added, so the operator's normal flow (OpenAlgo →
            # Zerodha login) no longer races the daemon thread and bails on
            # the first None api_key.
            from services.scanner_presubscribe import wire_pre_subscribe as _wire_pre_subscribe

            # Scanner service (Stage 1.5 item 5) — gated by SCANNER_ENABLED.
            # Off by default. When enabled, it subscribes to the ZMQ tick bus
            # and evaluates registered scan rules against incoming bars; it
            # does NOT touch the engine's order path (item 6 wires the
            # webhook poster as a scan_hit consumer).
            try:
                if os.environ.get("SCANNER_ENABLED", "false").lower() == "true":
                    from services.scanner_aggregator_symbols import (
                        compute_aggregator_symbols,
                    )
                    from services.scanner_service import ScannerService

                    # Issue #161: the scanner aggregator must track every
                    # symbol any downstream consumer queries. SCANNER_SYMBOLS
                    # alone is NOT enough — sector_follow's mapped sector
                    # indices (NIFTYAUTO/FMCG/IT/METAL/PSUBANK/PVTBANK +
                    # NIFTY/BANKNIFTY) and REGIME_SECTOR_SYMBOLS are
                    # WS-subscribed elsewhere but never seen by the
                    # aggregator. compute_aggregator_symbols unions all
                    # required sources and logs the per-source breakdown.
                    symbols = compute_aggregator_symbols()
                    if not symbols:
                        logger.warning(
                            "SCANNER_ENABLED=true but no symbols across "
                            "SCANNER_SYMBOLS + REGIME_SECTOR_SYMBOLS + sector_follow — "
                            "scanner will idle (nothing to watch)"
                        )
                    scanner_intervals = [
                        i.strip()
                        for i in os.environ.get("SCANNER_INTERVALS", "5m").split(",")
                        if i.strip()
                    ]
                    app.scanner_service = ScannerService(
                        symbols=symbols, intervals=scanner_intervals
                    )
                    # Publish the live singleton so in-process consumers
                    # (sector_follow Fix 1b reads today's aggregated bars) can
                    # reach it without an import cycle.
                    from services.scanner_service import set_scanner_service

                    set_scanner_service(app.scanner_service)
                    app.scanner_service.start()
                    logger.info(
                        "Scanner service started (symbols=%d, intervals=%s)",
                        len(symbols),
                        scanner_intervals,
                    )

                    # Issue #156 Phase 2 (R3): seed the aggregator's rolling
                    # bars from historify at boot so RSI(14)/SMA(20) windows
                    # are full by the time the first live tick arrives —
                    # eliminates the ~25k pandas_ta_classic verify_series
                    # warnings per restart AND the 100-min silent warmup
                    # period that looks identical to 'no setups today'.
                    # Non-blocking daemon: waits for the broker session, then
                    # folds last N min of 1m bars per symbol via
                    # aggregator.replay_bars (idempotent, ws_recovery's later
                    # replay never double-counts).
                    try:
                        from services.scanner_aggregator_seeder import (
                            init_scanner_aggregator_seeder,
                        )

                        # Issue #201: also pass the per-symbol 15m bar
                        # builders so the seeder pre-warms them from the
                        # same 1m series; otherwise the rules' 15m RSI(14)
                        # warm-up guard (needs 14 closed 15m bars =
                        # ~3h30min of live ticks) blocks every signal
                        # after a mid-session restart.
                        init_scanner_aggregator_seeder(
                            app.scanner_service.aggregator,
                            symbols,
                            bar_15m_history=getattr(app.scanner_service, "_bar_15m_history", None),
                        )
                    except Exception as e:
                        logger.error(f"Failed to start aggregator seeder: {e}")

                    # Pre-subscribe scanner symbols to the broker WebSocket so
                    # ticks flow from market open without waiting for a Chartink
                    # hit. Event-driven (see services/scanner_presubscribe.py):
                    # a connect callback re-subscribes the full universe every
                    # time the broker WS comes up — the first connect after the
                    # morning Zerodha login and every mid-day reconnect — and a
                    # short boot retry establishes the initial connection. The
                    # 5 index symbols mixed into SCANNER_SYMBOLS (NIFTY,
                    # BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50) are routed to
                    # NSE_INDEX automatically by the subscriber.
                    if symbols:
                        from services.scanner_presubscribe import scanner_pre_subscriber

                        _wire_pre_subscribe(
                            "scanner_pre_subscribe",
                            scanner_pre_subscriber,
                            list(symbols),
                            thread_name="ScannerPreSubscribe",
                        )

                    # Scanner WS liveness watchdog — recovers a stalled tick
                    # stream (open socket, silent feed) during market hours via
                    # soft (ws.close → reconnect) then hard (client re-init)
                    # recovery. See services/scanner_ws_watchdog.py.
                    if os.environ.get("SCANNER_WS_WATCHDOG_ENABLED", "true").lower() == "true":
                        try:
                            from database.auth_db import (
                                get_first_available_api_key,
                                verify_api_key,
                            )
                            from services.scanner_ws_watchdog import (
                                start_scanner_ws_watchdog,
                            )

                            _wd_key = get_first_available_api_key()
                            _wd_user = verify_api_key(_wd_key) if _wd_key else None
                            if _wd_user:
                                start_scanner_ws_watchdog(_wd_user, app=app)
                            else:
                                logger.warning(
                                    "Scanner WS watchdog: no API key at boot; not "
                                    "started (no session to monitor)"
                                )
                        except Exception as e:
                            logger.error("Failed to start scanner WS watchdog: %s", e)
                else:
                    logger.debug("Scanner service disabled (SCANNER_ENABLED!=true)")
            except Exception as e:
                logger.error(f"Failed to initialize Scanner service: {e}")

            # WS-reconnect recovery (Fix B-prime) — on every broker session
            # refresh (the broker_session_refreshed event from the Task 2
            # event-driven WS reinit), fetch the 1m bars missed during the feed
            # gap and replay them into the scanner aggregator so the scanner does
            # not silently warm up from scratch (the tick-starvation gap). No flag;
            # listens on the in-process event bus for the life of the process. See
            # services/ws_recovery_service.py.
            try:
                from services.ws_recovery_service import init_ws_recovery_service

                init_ws_recovery_service(app)
                logger.info("WS recovery service registered")
            except Exception as e:
                logger.error(f"Failed to initialize WS recovery service: {e}")

            # Issue #157 (R4 of #156): boot-time orphan-exit reconciliation.
            # Marks pre-existing trade_journal rows where exit_reason was set
            # but exit_price never landed (broker rejection at 15:14 IST etc.)
            # as 'abandoned_<original>' so the engine stops re-attempting them
            # on every restart. Idempotent; non-blocking daemon; waits for the
            # broker session before scanning.
            try:
                from services.orphan_exit_reconciliation_service import (
                    init_orphan_exit_reconciliation,
                )

                init_orphan_exit_reconciliation()
            except Exception as e:
                logger.error(f"Failed to initialize orphan-exit reconciliation: {e}")

            # Scanner history cache warm-up (Task 3) — pre-load daily/weekly
            # bars so the first scan does not pay per-symbol lazy-load latency.
            # Gated by SCANNER_HISTORY_WARMUP_ENABLED (default true). Runs on a
            # daemon thread so a slow/failed bulk DuckDB read never blocks boot;
            # the scanner can serve with an empty cache and lazy-load on demand.
            # Placed after the scanner pre-subscribe wiring above. See
            # services/scanner_history_provider.py:run_boot_warmup.
            try:
                import threading as _warmup_threading

                from services.scanner_history_provider import run_boot_warmup

                _warmup_threading.Thread(
                    target=run_boot_warmup,
                    daemon=True,
                    name="ScannerHistoryWarmup",
                ).start()
                logger.info("Scanner history warm-up thread started")
            except Exception as e:
                logger.error(f"Failed to start scanner history warm-up: {e}")

            # Stage 1.7 regime classifier — pre-subscribe NIFTY sector
            # indices on NSE_INDEX so the sector-rotation classifier has
            # live quotes (rather than falling back to historify EOD
            # close-to-close). Event-driven, same mechanism as the scanner
            # pre-subscribe above: a connect callback re-subscribes on every
            # broker WS connect+auth, with a short boot retry to establish the
            # initial connection. All REGIME_SECTOR_SYMBOLS are indices, so the
            # regime subscriber forces NSE_INDEX. Failures are warnings only —
            # the classifier degrades gracefully to historify EOD data.
            try:
                raw_sector_symbols = os.environ.get("REGIME_SECTOR_SYMBOLS", "")
                sector_symbols = [
                    s.strip().upper() for s in raw_sector_symbols.split(",") if s.strip()
                ]
                if sector_symbols:
                    from services.scanner_presubscribe import regime_pre_subscriber

                    _wire_pre_subscribe(
                        "regime_pre_subscribe",
                        regime_pre_subscriber,
                        list(sector_symbols),
                        thread_name="RegimeSectorPreSubscribe",
                    )
                else:
                    logger.debug(
                        "Regime sector pre-subscribe skipped (REGIME_SECTOR_SYMBOLS empty)"
                    )
            except Exception as e:
                logger.error(f"Failed to schedule regime sector pre-subscribe: {e}")

            # Scan-hit webhook poster (Stage 1.5 item 6) — gated by
            # SCAN_HIT_POSTER_ENABLED. Subscribes to ``scan_hit`` events
            # emitted by the scanner above. Default mode is ``shadow`` —
            # the consumer fires and logs but performs NO HTTP POST, so
            # turning the scanner on does not by itself route signals into
            # the engine. Operator flips to ``active`` via env + restart.
            try:
                if os.environ.get("SCAN_HIT_POSTER_ENABLED", "true").lower() == "true":
                    from services.scan_hit_poster import ScanHitPoster

                    app.scan_hit_poster = ScanHitPoster.from_env()
                    app.scan_hit_poster.start()
                    logger.info(
                        "Scan-hit poster started (mode=%s)",
                        app.scan_hit_poster.mode,
                    )
                else:
                    logger.debug("Scan-hit poster disabled (SCAN_HIT_POSTER_ENABLED!=true)")
            except Exception as e:
                logger.error(f"Failed to initialize Scan-hit poster: {e}")

            # P0 (2026-06-01 NBCC EOD incident): rehydrate the simplified
            # engine's in-memory positions from today's open trade_journal
            # rows BEFORE the EOD watchdog scheduler kicks in. A mid-day
            # restart wipes the engine's `positions` dict; without this, the
            # engine can't issue an EOD exit even when ticks resume, and the
            # watchdog can only flatten via the broker (no engine-side
            # cleanup). Pulling this in here also force-instantiates the
            # engine singleton, which is what the watchdog needs to share an
            # api_key map with the live order path.
            try:
                from services.simplified_stock_engine_service import (
                    get_simplified_stock_engine_service,
                )

                _engine_svc = get_simplified_stock_engine_service()
                rehydrated = _engine_svc.rehydrate_positions_from_journal()
                logger.info(
                    "Simplified engine rehydrate complete (positions_restored=%d)",
                    rehydrated,
                )
            except Exception:
                logger.exception("Simplified engine rehydrate failed at startup")

            # EOD watchdog (P0 — 2026-06-01 NBCC fix). Schedules one cron job
            # per intraday strategy at its declared eod_exit_time. Independent
            # of the broker WebSocket tick stream — it's the safety net for
            # the case where ticks die before EOD and the tick-driven
            # `_maybe_flatten_eod` path can't fire. Must run AFTER the
            # strategies registry is loaded (module import at top of file
            # already did that) and AFTER DB tables exist (rehydrate above
            # depends on trade_journal).
            try:
                from services.eod_watchdog_service import start_eod_watchdog

                _wd_result = start_eod_watchdog()
                if _wd_result.get("started"):
                    logger.info(
                        "EOD watchdog started (jobs=%d, skipped=%d)",
                        len(_wd_result.get("jobs") or []),
                        len(_wd_result.get("skipped") or []),
                    )
                elif _wd_result.get("disabled"):
                    logger.info("EOD watchdog disabled via SIMPLIFIED_ENGINE_EOD_WATCHDOG_ENABLED")
                else:
                    logger.warning("EOD watchdog did not start (already running or empty)")
            except Exception:
                logger.exception("Failed to start EOD watchdog")

            # Phase A — News ingest sidecar (writes to market_intel; no
            # consumers yet). Pure ingestion: every 5 min, 08:00–15:30 IST
            # mon-fri. Boot-safe — failures here log and continue.
            try:
                from services.news_ingest_service import (
                    start_news_ingest_scheduler,
                )

                _news_result = start_news_ingest_scheduler()
                logger.info(f"News ingest scheduler: {_news_result}")
            except Exception:
                logger.exception("Failed to start news ingest scheduler")

            # Stage 2 part 2 — journal reflection (LLM-based pattern synthesis)
            # at 16:00 IST mon-fri, after EOD watchdog has flattened intraday
            # positions and the day's trade_journal rows are closed out.
            # Independent of the order path — purely forensic, persists into
            # journal_reflection. Failures here must not block boot.
            try:
                from services.journal_reflection_service import (
                    schedule_nightly_reflection,
                )

                schedule_nightly_reflection()
            except Exception:
                logger.exception("Failed to schedule journal reflection")

            # Auto-reconnect the WhatsApp bot if a paired session is persisted.
            # Without this, every server restart would leave is_ready()=False
            # and every /notify call would 409 "pair first" — even though the
            # encrypted session blob is sitting in openalgo.db ready to use.
            # We do this on a background thread so a slow WhatsApp handshake
            # never delays the Flask boot.
            def _autostart_whatsapp_bot():
                try:
                    from database.whatsapp_db import get_bot_config
                    from services.whatsapp_bot_service import whatsapp_bot_service

                    if not get_bot_config().get("is_paired"):
                        logger.debug("WhatsApp: no paired session, skipping auto-start")
                        return
                    ok, msg = whatsapp_bot_service.start_bot()
                    if ok:
                        logger.info("WhatsApp bot auto-started from persisted session")
                    else:
                        logger.warning("WhatsApp bot auto-start failed: %s", msg)
                except Exception:
                    logger.exception("WhatsApp bot auto-start crashed")

            import threading as _threading

            _threading.Thread(
                target=_autostart_whatsapp_bot,
                daemon=True,
                name="WhatsAppAutoStart",
            ).start()

            # Auto-start analyzer mode services (depends on DB being ready)
            try:
                from database.settings_db import get_analyze_mode

                if get_analyze_mode():
                    from sandbox.execution_thread import start_execution_engine
                    from sandbox.squareoff_thread import start_squareoff_scheduler

                    def start_engine():
                        success, message = start_execution_engine()
                        return ("execution_engine", success, message)

                    def start_scheduler():
                        success, message = start_squareoff_scheduler()
                        return ("squareoff_scheduler", success, message)

                    def run_catchup():
                        from sandbox.position_manager import catchup_missed_settlements

                        catchup_missed_settlements()
                        return ("catchup_settlement", True, "Completed")

                    with ThreadPoolExecutor(max_workers=3) as executor:
                        futures = [
                            executor.submit(start_engine),
                            executor.submit(start_scheduler),
                            executor.submit(run_catchup),
                        ]
                        for future in as_completed(futures):
                            try:
                                service_name, success, message = future.result()
                                if service_name == "execution_engine":
                                    if success:
                                        logger.debug(
                                            "Execution engine auto-started (Analyzer mode is ON)"
                                        )
                                    else:
                                        logger.warning(
                                            f"Failed to auto-start execution engine: {message}"
                                        )
                                elif service_name == "squareoff_scheduler":
                                    if success:
                                        logger.debug(
                                            "Square-off scheduler auto-started (Analyzer mode is ON)"
                                        )
                                    else:
                                        logger.warning(
                                            f"Failed to auto-start square-off scheduler: {message}"
                                        )
                                elif service_name == "catchup_settlement":
                                    logger.debug("Catch-up settlement check completed on startup")
                            except Exception as e:
                                logger.error(f"Error starting service: {e}")
            except Exception as e:
                logger.error(f"Error checking analyzer mode on startup: {e}")

            # Auto-start Telegram bot if it was active (after DB tables exist)
            try:
                import sys

                bot_config = get_bot_config()
                if bot_config.get("is_active") and bot_config.get("bot_token"):
                    logger.debug("Auto-starting Telegram bot (background)...")

                    if "eventlet" in sys.modules:
                        success, message = telegram_bot_service.initialize_bot_sync(
                            token=bot_config["bot_token"]
                        )
                        if success:
                            success, message = telegram_bot_service.start_bot()
                            if success:
                                logger.debug(f"Telegram bot auto-started successfully: {message}")
                            else:
                                logger.error(f"Failed to auto-start Telegram bot: {message}")
                        else:
                            logger.error(f"Failed to initialize Telegram bot: {message}")
                    else:
                        import asyncio

                        try:
                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)
                            try:
                                success, message = loop.run_until_complete(
                                    telegram_bot_service.initialize_bot(
                                        token=bot_config["bot_token"]
                                    )
                                )
                            finally:
                                loop.close()

                            if success:
                                success, message = telegram_bot_service.start_bot()
                                if success:
                                    logger.debug(
                                        f"Telegram bot auto-started successfully: {message}"
                                    )
                                else:
                                    logger.error(f"Failed to auto-start Telegram bot: {message}")
                            else:
                                logger.error(f"Failed to initialize Telegram bot: {message}")
                        except Exception as e:
                            logger.error(f"Error in Telegram bot startup: {e}")
            except Exception as e:
                logger.error(f"Error auto-starting Telegram bot: {e}")

    threading.Thread(target=_init_databases_and_schedulers, daemon=True).start()


# Module-level initialization — skipped when OPENALGO_TESTING=1 so that
# ``from app import create_app`` in test/harness.py is safe (no singleton
# check, no background daemons, no WS proxy).
if os.environ.get("OPENALGO_TESTING") != "1":
    # Refuse to boot a second concurrent instance before any Flask init — two
    # writers on the SQLite DBs corrupt them. See utils/singleton_guard.py.
    from utils.singleton_guard import check_singleton_or_abort as _check_singleton_or_abort

    _check_singleton_or_abort()

    app = create_app()

    # Explicitly call the setup environment function
    setup_environment(app)

    # Restore caches from database in background (not needed until first trade/lookup)
    import threading

    def _restore_caches_background():
        # Wait for DB tables to be created before querying
        app.db_ready.wait()
        with app.app_context():
            try:
                from database.cache_restoration import restore_all_caches

                cache_result = restore_all_caches()

                if cache_result["success"]:
                    symbol_count = cache_result["symbol_cache"].get("symbols_loaded", 0)
                    auth_count = cache_result["auth_cache"].get("tokens_loaded", 0)
                    if symbol_count > 0 or auth_count > 0:
                        logger.debug(
                            f"Cache restoration: {symbol_count} symbols, {auth_count} auth tokens"
                        )
            except Exception as e:
                logger.debug(f"Cache restoration skipped: {e}")

    threading.Thread(target=_restore_caches_background, daemon=True).start()

    # Integrate the WebSocket proxy server with the Flask app
    # Check if running in Docker (standalone mode) or local (integrated mode)
    # Docker is detected by checking for /.dockerenv file or APP_MODE override
    is_docker = (
        os.path.exists("/.dockerenv")
        or os.environ.get("APP_MODE", "").strip().strip("'\"") == "standalone"
    )

    if is_docker:
        logger.debug(
            "Running in Docker/standalone mode - WebSocket server started separately by start.sh"
        )
    else:
        # Under gunicorn+eventlet, start_websocket_proxy() spawns a child *process*
        # (not a thread) so the WS asyncio loop never shares an eventlet hub with
        # gunicorn — closes the greenlet.error cross-thread crash class entirely
        # (including GitHub issue #1421). Under the dev server (no eventlet) it
        # still uses a real OS thread, as before.
        logger.debug("Starting WebSocket proxy")
        start_websocket_proxy(app)


def _warn_if_dirty_working_tree():
    """Log a WARNING at boot if the git working tree has uncommitted changes.

    Catches code edits an operator (or an automated task) left behind without
    committing. Non-blocking: never exits the process. Gated by
    OPENALGO_BOOT_DIRTY_CHECK_ENABLED (default True) so it can be silenced if
    it becomes noisy.
    """
    if os.getenv("OPENALGO_BOOT_DIRTY_CHECK_ENABLED", "True").lower() not in ("true", "1", "t"):
        return
    try:
        import subprocess  # nosec B404

        repo_root = os.path.dirname(os.path.abspath(__file__))
        result = subprocess.run(  # nosec
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        changes = result.stdout.strip()
        if changes:
            logger.warning(
                "Dirty working tree detected at boot:\n%s\n"
                "Operator: review any code changes that should have been committed.",
                changes,
            )
    except Exception as e:
        logger.debug(f"Dirty working tree check skipped (git unavailable?): {e}")


# Start Flask development server with SocketIO support if directly executed
if __name__ == "__main__":
    _warn_if_dirty_working_tree()
    host_ip = os.getenv("FLASK_HOST_IP", "127.0.0.1")
    port = int(os.getenv("FLASK_PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "False").lower() in ("true", "1", "t")

    # Refuse to run the Werkzeug debugger on a non-loopback interface.
    # Werkzeug's interactive debugger is an RCE primitive — exposing it on a
    # public or LAN address is a critical risk, and a surprisingly common
    # misconfiguration (FLASK_DEBUG=True left on + FLASK_HOST_IP=0.0.0.0).
    # Users who explicitly need debug on a trusted LAN can set
    # FLASK_DEBUG_ALLOW_EXTERNAL=true to opt out of this guard.
    _LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", ""}
    _allow_external_debug = os.getenv("FLASK_DEBUG_ALLOW_EXTERNAL", "False").lower() in (
        "true",
        "1",
        "t",
    )
    if debug and host_ip not in _LOOPBACK_HOSTS and not _allow_external_debug:
        sys.stderr.write(
            "\n"
            "\033[91m\033[1m"
            "REFUSING TO START: FLASK_DEBUG=True with FLASK_HOST_IP="
            f"{host_ip!r}\033[0m\n"
            "\033[91m"
            "The Werkzeug interactive debugger is an RCE primitive and must\n"
            "never be reachable from the network. Fix one of the following:\n"
            "  1. Set FLASK_DEBUG=False in .env (recommended for anything\n"
            "     beyond local development).\n"
            "  2. Set FLASK_HOST_IP=127.0.0.1 in .env to bind to loopback.\n"
            "  3. If you truly need debug on a trusted LAN, set\n"
            "     FLASK_DEBUG_ALLOW_EXTERNAL=true in .env to override this\n"
            "     guard. You are responsible for the consequences.\n"
            "\033[0m\n"
        )
        sys.exit(1)

    # Start ngrok tunnel if enabled
    should_start_ngrok = True
    if debug:
        should_start_ngrok = os.environ.get("WERKZEUG_RUN_MAIN") == "true"

    if should_start_ngrok and os.getenv("NGROK_ALLOW", "FALSE").upper() == "TRUE":
        from utils.ngrok_manager import start_ngrok_tunnel

        start_ngrok_tunnel(port)

    # Exclude strategies and logs directories from reloader
    reloader_options = {
        "exclude_patterns": [
            "*/strategies/*",
            "*/log/*",
            "*.log",
            "*.bak",
        ]
    }
    # Suppress Flask/Werkzeug's default startup banner — our banner replaces it
    import flask.cli

    flask.cli.show_server_banner = lambda *_: None

    # Print startup banner NOW — right before the server starts accepting connections.
    # When the user sees this banner, the portal is ready to load.
    if not debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        from utils.version import get_version as _get_ver

        _ver = _get_ver()
        _dip = host_ip
        if host_ip == "0.0.0.0":  # nosec B104
            import socket as _sk

            try:
                _s = _sk.socket(_sk.AF_INET, _sk.SOCK_DGRAM)
                _s.connect(("8.8.8.8", 80))
                _dip = _s.getsockname()[0]
                _s.close()
            except Exception:
                _dip = "127.0.0.1"
        _wu = f"http://{_dip}:{port}"
        _wsu = f"ws://{_dip}:{os.getenv('WEBSOCKET_PORT', 8765)}"
        _du = "https://docs.openalgo.in"
        G, C, M, W, Y, R, BD, DM = (
            "\033[92m",
            "\033[96m",
            "\033[95m",
            "\033[97m",
            "\033[93m",
            "\033[0m",
            "\033[1m",
            "\033[2m",
        )
        _ae = re.compile(r"\x1B\[[0-9;]*m")

        def _vl(t):
            return len(_ae.sub("", t))

        _t = f" OpenAlgo v{_ver} "
        _sl = "Your Personal Algo Trading Platform"
        _samps = [
            "",
            _sl,
            f"{W}{BD}Endpoints{R}",
            f"{W}Web App{R}    {C}{_wu}{R}",
            f"{W}WebSocket{R}  {M}{_wsu}{R}",
            f"{W}Docs{R}       {Y}{_du}{R}",
            f"{W}Status{R}     {G}{BD}Ready{R}",
        ]
        _iw = max(50, max((_vl(s) for s in _samps), default=0))
        _W = max(_iw + 4, len(_t) + 5)
        _enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        try:
            "\u256d\u256e\u2570\u256f\u2502\u2500".encode(_enc)
            TL, TR, BL, BR, H, V = "\u256d", "\u256e", "\u2570", "\u256f", "\u2500", "\u2502"
        except Exception:
            TL, TR, BL, BR, H, V = "+", "+", "+", "+", "-", "|"

        def _ml(t=""):
            p = max(_W - 4 - _vl(t), 0)
            return f"{C}{V}{R} {t}{' ' * p} {C}{V}{R}"

        _slp = max((_W - 4 - _vl(_sl)) // 2, 0)
        _srp = max(_W - 4 - _vl(_sl) - _slp, 0)
        _td = max(0, _W - 5 - len(_t))
        print(
            "\n".join(
                [
                    "",
                    f"{C}{TL}{H * 3}{G}{BD}{_t}{R}{C}{H * _td}{TR}{R}",
                    _ml(),
                    f"{C}{V}{R} {' ' * _slp}{DM}{_sl}{R}{' ' * _srp} {C}{V}{R}",
                    _ml(),
                    _ml(f"{W}{BD}Endpoints{R}"),
                    _ml(f"{W}Web App{R}    {C}{_wu}{R}"),
                    _ml(f"{W}WebSocket{R}  {M}{_wsu}{R}"),
                    _ml(f"{W}Docs{R}       {Y}{_du}{R}"),
                    _ml(),
                    _ml(f"{W}Status{R}     {G}{BD}Ready{R}"),
                    _ml(),
                    f"{C}{BL}{H * (_W - 2)}{BR}{R}",
                    "",
                ]
            ),
            flush=True,
        )

    socketio.run(
        app,
        host=host_ip,
        port=port,
        debug=debug,
        reloader_options=reloader_options,
        allow_unsafe_werkzeug=True,
    )
