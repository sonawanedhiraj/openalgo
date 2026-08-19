"""Tests for the headless Kite web-login flow (issue #654).

``services.zerodha_web_login.fetch_request_token`` is pure and Flask-free — it
takes credentials + an injected ``httpx``-shaped client and returns
``(request_token, error)``. These tests drive it with a scripted fake client so
no network is touched, covering the happy path (login → twofa → redirect chain →
request_token) and each failure branch returning ``(None, reason)``.
"""

from __future__ import annotations

import pyotp
import pytest

from services.zerodha_web_login import fetch_request_token

TEST_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"  # pragma: allowlist secret
API_KEY = "testapikey"  # pragma: allowlist secret
REDIRECT = "http://127.0.0.1:5000/zerodha/callback"


class FakeResponse:
    def __init__(self, *, status=200, json_data=None, location=None, raise_exc=None):
        self.status_code = status
        self._json = json_data or {}
        self.headers = {"location": location} if location else {}
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc is not None:
            raise self._raise_exc

    def json(self):
        return self._json

    @property
    def is_redirect(self):
        return 300 <= self.status_code < 400


class FakeClient:
    """Scripted client: each POST/GET pops the next queued response.

    ``post_responses`` keyed by URL substring; ``get_responses`` is an ordered
    list consumed per connect/login hop.
    """

    def __init__(self, post_responses, get_responses):
        self._post = post_responses
        self._get = list(get_responses)
        self.posted = []
        self.closed = False

    def post(self, url, data=None):
        self.posted.append((url, data))
        for key, resp in self._post.items():
            if key in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"unexpected POST {url}")

    def get(self, url):
        if not self._get:
            raise AssertionError(f"unexpected GET {url} (no queued responses)")
        resp = self._get.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    def close(self):
        self.closed = True


def _happy_client(final_location=f"{REDIRECT}?request_token=RT123&action=login&status=success"):
    return FakeClient(
        post_responses={
            "/api/login": FakeResponse(json_data={"data": {"request_id": "REQ1"}}),
            "/api/twofa": FakeResponse(json_data={"data": {"profile": {}}}),
        },
        get_responses=[FakeResponse(status=302, location=final_location)],
    )


def test_happy_path_returns_request_token():
    client = _happy_client()
    token, error = fetch_request_token("AB1234", "pw", TEST_SECRET, API_KEY, client=client)
    assert error is None
    assert token == "RT123"
    # The TOTP posted must be a valid code for the secret.
    twofa_post = next(d for u, d in client.posted if "twofa" in u)
    assert pyotp.TOTP(TEST_SECRET).verify(twofa_post["twofa_value"], valid_window=1)
    assert twofa_post["twofa_type"] == "totp"


def test_intermediate_redirect_hop_then_token():
    """A finish/consent hop before the redirect_url is followed."""
    client = FakeClient(
        post_responses={
            "/api/login": FakeResponse(json_data={"data": {"request_id": "REQ1"}}),
            "/api/twofa": FakeResponse(json_data={"data": {}}),
        },
        get_responses=[
            FakeResponse(status=302, location="/connect/finish?api_key=x"),
            FakeResponse(status=302, location=f"{REDIRECT}?request_token=RT999&status=success"),
        ],
    )
    token, error = fetch_request_token("AB1234", "pw", TEST_SECRET, API_KEY, client=client)
    assert error is None
    assert token == "RT999"


def test_missing_inputs_rejected():
    token, error = fetch_request_token("", "pw", TEST_SECRET, API_KEY, client=_happy_client())
    assert token is None
    assert "Missing" in error


def test_login_no_request_id():
    client = FakeClient(
        post_responses={"/api/login": FakeResponse(json_data={"data": {}})},
        get_responses=[],
    )
    token, error = fetch_request_token("AB1234", "badpw", TEST_SECRET, API_KEY, client=client)
    assert token is None
    assert "request_id" in error


def test_login_http_error():
    client = FakeClient(
        post_responses={
            "/api/login": FakeResponse(status=403, raise_exc=RuntimeError("forbidden"))
        },
        get_responses=[],
    )
    token, error = fetch_request_token("AB1234", "pw", TEST_SECRET, API_KEY, client=client)
    assert token is None
    assert "api/login" in error


def test_twofa_failure():
    client = FakeClient(
        post_responses={
            "/api/login": FakeResponse(json_data={"data": {"request_id": "REQ1"}}),
            "/api/twofa": FakeResponse(status=400, raise_exc=RuntimeError("wrong totp")),
        },
        get_responses=[],
    )
    token, error = fetch_request_token("AB1234", "pw", TEST_SECRET, API_KEY, client=client)
    assert token is None
    assert "twofa" in error


def test_connect_login_no_token():
    """connect/login terminates without ever surfacing a request_token."""
    client = FakeClient(
        post_responses={
            "/api/login": FakeResponse(json_data={"data": {"request_id": "REQ1"}}),
            "/api/twofa": FakeResponse(json_data={"data": {}}),
        },
        get_responses=[FakeResponse(status=200)],
    )
    token, error = fetch_request_token("AB1234", "pw", TEST_SECRET, API_KEY, client=client)
    assert token is None
    assert "request_token" in error


def test_bad_totp_secret():
    client = FakeClient(
        post_responses={"/api/login": FakeResponse(json_data={"data": {"request_id": "REQ1"}})},
        get_responses=[],
    )
    token, error = fetch_request_token("AB1234", "pw", "not base32!!!", API_KEY, client=client)
    assert token is None
    assert "TOTP" in error
