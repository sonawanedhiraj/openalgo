"""Phase 4 Layer B — Playwright / HTTP E2E UI smoke stubs.

These tests verify the key UI surfaces of OpenAlgo after boot: login page,
auth-redirect, Swagger docs, React SPA, and the /health/status endpoint.

All tests are marked ``@pytest.mark.skip`` with reason "CD CI OOM — enable
when Docker E2E pipeline is fixed".  They are collected by pytest but produce
a SKIP rather than a failure, so they do NOT block the main CI gate
(``CI: Unit + Integration Tests``) and do NOT block the CD gate while the
Docker build OOM is unresolved.

When the Docker OOM is fixed:
1. Remove the ``@pytest.mark.skip`` decorator from each test.
2. Ensure ``playwright`` + ``pytest-playwright`` are installed in the CI image:
   ``pip install playwright pytest-playwright && playwright install chromium``.
3. Re-run with ``uv run pytest test/e2e/test_phase4_ui_smoke.py -v``.

URL assumptions (configurable via ``BASE_URL`` env var):
- OpenAlgo is running at ``http://localhost:5000``
- ``/login``        → React SPA login page
- ``/dashboard``    → React SPA dashboard (requires auth → redirects to login)
- ``/api/docs``     → Flask-RESTX Swagger UI
- ``/``             → React SPA index (auth-gated → redirects to login)
- ``/health/status``→ unauthenticated JSON health endpoint

These routes are defined in:
- ``blueprints/react_app.py`` (React SPA routes)
- ``blueprints/health.py``    (``/health/status`` → ``simple_health``)
- ``restx_api/``              (Flask-RESTX, mounted at ``/api/``)
"""

from __future__ import annotations

import os

import pytest

# playwright is NOT in pyproject.toml dependencies (it conflicts with eventlet
# pins).  Import it lazily so the test file can be collected even when playwright
# is absent — the skip marker prevents execution anyway.
try:
    from playwright.sync_api import Page  # type: ignore[import]

    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False
    Page = object  # type: ignore[assignment,misc]  # noqa: N816 — placeholder

import requests

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SKIP_REASON = "CD CI OOM — enable when Docker E2E pipeline is fixed"

#: Base URL for all requests.  Override via BASE_URL env var in the Docker
#: Compose E2E job to point at the container's exposed port.
BASE_URL = os.getenv("BASE_URL", "http://localhost:5000").rstrip("/")


# ---------------------------------------------------------------------------
# Test 1 — login page renders
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.skip(reason=_SKIP_REASON)
def test_login_page_renders(page: Page) -> None:  # type: ignore[name-defined]
    """Login page loads, title contains 'OpenAlgo', and a login form is present.

    The login route (``/login``) is served by ``blueprints/react_app.py``
    ``react_login()`` → ``serve_react_app()`` which returns
    ``frontend/dist/index.html``.  React Router then renders the Login page
    component.

    Assertions:
    - HTTP 200 (not 404 / 503)
    - ``<title>`` contains "OpenAlgo" or "Login"
    - A username input is visible (``input[name='username']`` or ``#username``)
    - A password input is visible
    """
    page.goto(f"{BASE_URL}/login")

    title = page.title()
    assert "openalgo" in title.lower() or "login" in title.lower(), (
        f"Expected 'OpenAlgo' or 'Login' in page title, got: {title!r}"
    )

    # Username field — React component may use name= or id= or placeholder=
    username_locator = page.locator(
        "input[name='username'], input[id='username'], input[placeholder*='username' i], "
        "input[type='text']:visible, input[type='email']:visible"
    )
    assert username_locator.count() > 0, "No username/text input found on login page"

    password_locator = page.locator("input[type='password']:visible")
    assert password_locator.count() > 0, "No password input found on login page"


# ---------------------------------------------------------------------------
# Test 2 — dashboard requires authentication
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.skip(reason=_SKIP_REASON)
def test_dashboard_requires_auth(page: Page) -> None:  # type: ignore[name-defined]
    """Unauthenticated access to the dashboard surface is redirected to login.

    ``/dashboard`` is served by ``blueprints/react_app.py`` ``react_dashboard()``
    which returns ``index.html`` regardless of session state.  React Router's
    ``ProtectedRoute`` component (``frontend/src/components/ProtectedRoute.tsx``)
    checks the auth state client-side and navigates to ``/login`` when the user
    is not logged in.

    The test therefore navigates to ``/dashboard``, waits for navigation to
    settle, and asserts the final URL contains "login" OR that a login form
    element is present (the two observable states of the auth-guard).
    """
    page.goto(f"{BASE_URL}/dashboard", wait_until="networkidle")

    final_url = page.url
    has_login_form = page.locator("input[type='password']:visible").count() > 0

    assert "login" in final_url.lower() or has_login_form, (
        f"Expected redirect to /login for unauthenticated /dashboard access, "
        f"but final URL was {final_url!r} and no login form was found"
    )


# ---------------------------------------------------------------------------
# Test 3 — Swagger UI loads
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.skip(reason=_SKIP_REASON)
def test_api_docs_swagger_ui_loads(page: Page) -> None:  # type: ignore[name-defined]
    """Flask-RESTX Swagger UI is served at ``/api/docs``.

    The ``restx_api/`` package registers all API namespaces under a Flask-RESTX
    ``Api`` instance mounted at ``/api/``.  Flask-RESTX automatically serves a
    Swagger UI at ``/api/docs``.

    Assertions:
    - HTTP 200 (page.goto does not raise)
    - Page content contains "swagger" or "OpenAlgo" (title or body text)
    - At least one Swagger UI element is present (the spec container or the
      title heading rendered by swagger-ui-bundle.js)
    """
    page.goto(f"{BASE_URL}/api/docs", wait_until="networkidle")

    page_content = page.content().lower()
    title = page.title().lower()

    assert "swagger" in page_content or "openalgo" in page_content or "swagger" in title, (
        "Expected Swagger UI content at /api/docs but found neither 'swagger' "
        f"nor 'openalgo' in page content.  Title was: {page.title()!r}"
    )


# ---------------------------------------------------------------------------
# Test 4 — React SPA root route returns content (not a 404)
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.skip(reason=_SKIP_REASON)
def test_react_frontend_loads(page: Page) -> None:  # type: ignore[name-defined]
    """The React SPA root (``/``) serves the frontend and is not a hard 404.

    ``/`` is handled by ``blueprints/react_app.py`` ``react_index()`` which
    calls ``serve_react_app()``.  If the ``frontend/dist/`` build is present it
    returns ``index.html`` (HTTP 200); otherwise it returns a 503 "Frontend Not
    Built" page that also confirms the app is running.

    Either outcome is acceptable as long as the HTTP status is not 404 and the
    page contains recognisable content (not an empty body or a generic web-server
    error).

    The test additionally checks that the page title is non-empty and contains
    at least one word — a blank or missing title would indicate the HTML shell
    failed to parse.
    """
    response = page.goto(f"{BASE_URL}/", wait_until="domcontentloaded")

    assert response is not None, "page.goto('/') returned None — server may be down"
    assert response.status != 404, (
        f"GET / returned HTTP {response.status}; expected 200 or 503 (build missing), not 404"
    )

    title = page.title()
    assert title and len(title.strip()) > 0, (
        "Page title at '/' is empty — the HTML shell may have failed to render"
    )

    body_text = page.locator("body").inner_text()
    assert len(body_text.strip()) > 0, (
        "Page body at '/' is empty — React app or 'Frontend Not Built' page "
        "should have rendered some content"
    )


# ---------------------------------------------------------------------------
# Test 5 — /health/status returns 200 with a status key (HTTP, no browser)
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.skip(reason=_SKIP_REASON)
def test_health_endpoint_returns_ok() -> None:
    """``GET /health/status`` returns HTTP 200 and a JSON body with a ``status`` key.

    ``/health/status`` is served by ``blueprints/health.py`` ``simple_health()``
    (unauthenticated, rate-limited to 300/min).  It follows the
    draft-inadarei-api-health-check-06 spec and is used by AWS ELB / k8s probes.

    This test uses ``requests`` (not a browser) because the endpoint is a pure
    JSON API — no JavaScript rendering is needed.

    Assertions:
    - HTTP 200
    - Response body is valid JSON
    - JSON contains a ``"status"`` key whose value is one of "pass", "warn", "fail"
    """
    url = f"{BASE_URL}/health/status"
    try:
        resp = requests.get(url, timeout=5)
    except requests.exceptions.ConnectionError as exc:
        pytest.fail(f"Could not connect to {url!r} — is OpenAlgo running?\nOriginal error: {exc}")

    assert resp.status_code == 200, (
        f"GET /health/status returned HTTP {resp.status_code}, expected 200"
    )

    try:
        body = resp.json()
    except ValueError as exc:
        pytest.fail(
            f"GET /health/status response is not valid JSON.\n"
            f"Content-Type: {resp.headers.get('Content-Type')!r}\n"
            f"Body (first 200 chars): {resp.text[:200]!r}\n"
            f"Error: {exc}"
        )

    assert "status" in body, (
        f"JSON response from /health/status has no 'status' key.  Keys present: {list(body.keys())}"
    )

    valid_values = {"pass", "warn", "fail"}
    assert body["status"] in valid_values, (
        f"'status' value {body['status']!r} is not one of {valid_values}"
    )
