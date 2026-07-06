"""Tests for the POST /strategies/api/<name>/mode endpoint + CLI (issue #162 S6).

These two surfaces are the operator-facing entry points for the flip path.
Both must route through ``services.strategy_mode_service.flip_mode`` so the
preflight + audit + event always fire.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from services.strategy_mode_service import FlipOutcome, _cli_main

# --------------------------------------------------------------------------- #
# HTTP endpoint
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(monkeypatch):
    """A Flask test client with the dashboard blueprint registered.

    The dashboard routes are decorated with ``check_session_validity``, which
    inspects ``utils.session.is_session_valid`` and redirects to /login on
    failure. We mock the validity probe to always return True so the route
    handlers actually run.
    """
    import flask

    # Bypass session validation for the tests — the surface under test is
    # the route handler, not the auth decorator.
    monkeypatch.setattr("utils.session.is_session_valid", lambda: True)

    from blueprints.strategies_dashboard_api import strategies_dashboard_bp

    app = flask.Flask(__name__)
    app.config["SECRET_KEY"] = "test-only"  # pragma: allowlist secret
    app.config["TESTING"] = True
    app.register_blueprint(strategies_dashboard_bp)
    return app.test_client()


def test_post_mode_accepted_returns_202(client, tmp_path, monkeypatch):
    """Happy path: preflight passes → 202 with accepted=True payload."""
    # Make the strategy dir exist so the existence check passes.
    monkeypatch.setattr(
        "blueprints.strategies_dashboard_api._STRATEGIES_DIR",
        tmp_path,
    )
    (tmp_path / "sector_follow_cap5_vol").mkdir()

    accepted_outcome = FlipOutcome(
        accepted=True,
        strategy_name="sector_follow_cap5_vol",
        target_mode="live",
        previous_mode="sandbox",
        new_mode="live",
        blockers=[],
        warnings=[],
        audit_id=42,
    )
    with patch(
        "services.strategy_mode_service.flip_mode",
        return_value=accepted_outcome,
    ) as flip:
        resp = client.post(
            "/strategies/api/sector_follow_cap5_vol/mode",
            json={"mode": "live", "notes": "going live"},
        )

    assert resp.status_code == 202
    payload = resp.get_json()
    assert payload["status"] == "success"
    assert payload["accepted"] is True
    assert payload["new_mode"] == "live"
    assert payload["audit_id"] == 42
    flip.assert_called_once()
    assert flip.call_args.kwargs["target_mode"] == "live"
    assert flip.call_args.kwargs["notes"] == "going live"
    assert flip.call_args.kwargs["flipped_by"].startswith("ui:")


def test_post_mode_blocked_returns_409(client, tmp_path, monkeypatch):
    """Preflight refuses → 409 + blockers list."""
    monkeypatch.setattr(
        "blueprints.strategies_dashboard_api._STRATEGIES_DIR",
        tmp_path,
    )
    (tmp_path / "sector_follow_cap5_vol").mkdir()

    blocked_outcome = FlipOutcome(
        accepted=False,
        strategy_name="sector_follow_cap5_vol",
        target_mode="live",
        previous_mode="sandbox",
        new_mode="sandbox",  # unchanged
        blockers=["Index intraday aggregator empty for 8/8 sector indices"],
        warnings=[],
        audit_id=99,
    )
    with patch(
        "services.strategy_mode_service.flip_mode",
        return_value=blocked_outcome,
    ):
        resp = client.post(
            "/strategies/api/sector_follow_cap5_vol/mode",
            json={"mode": "live"},
        )

    assert resp.status_code == 409
    payload = resp.get_json()
    assert payload["status"] == "blocked"
    assert payload["accepted"] is False
    assert payload["new_mode"] == "sandbox"  # mode unchanged
    assert "Index intraday aggregator empty for 8/8 sector indices" in payload["blockers"]


def test_post_mode_invalid_mode_returns_400(client, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "blueprints.strategies_dashboard_api._STRATEGIES_DIR",
        tmp_path,
    )
    (tmp_path / "x").mkdir()

    resp = client.post("/strategies/api/x/mode", json={"mode": "yolo"})
    assert resp.status_code == 400


def test_post_mode_missing_body_returns_400(client, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "blueprints.strategies_dashboard_api._STRATEGIES_DIR",
        tmp_path,
    )
    (tmp_path / "x").mkdir()

    resp = client.post("/strategies/api/x/mode")
    assert resp.status_code == 400


def test_post_mode_unknown_strategy_returns_404(client, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "blueprints.strategies_dashboard_api._STRATEGIES_DIR",
        tmp_path,
    )
    # No strategy dir created.
    resp = client.post("/strategies/api/missing/mode", json={"mode": "live"})
    assert resp.status_code == 404


def test_get_mode_audit_returns_recent_rows(client, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "blueprints.strategies_dashboard_api._STRATEGIES_DIR",
        tmp_path,
    )
    (tmp_path / "sector_follow_cap5_vol").mkdir()

    fake_rows = [
        {"id": 2, "accepted": False, "blockers": ["x"], "target_mode": "live"},
        {"id": 1, "accepted": True, "blockers": [], "target_mode": "sandbox"},
    ]
    with patch(
        "database.strategy_mode_audit_db.list_attempts",
        return_value=fake_rows,
    ) as list_fn:
        resp = client.get("/strategies/api/sector_follow_cap5_vol/mode/audit?limit=10")

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["status"] == "success"
    assert payload["data"]["rows"] == fake_rows
    list_fn.assert_called_once()
    assert list_fn.call_args.kwargs["strategy_name"] == "sector_follow_cap5_vol"


def test_get_mode_audit_clamps_limit(client, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "blueprints.strategies_dashboard_api._STRATEGIES_DIR",
        tmp_path,
    )
    (tmp_path / "x").mkdir()

    with patch(
        "database.strategy_mode_audit_db.list_attempts",
        return_value=[],
    ) as list_fn:
        client.get("/strategies/api/x/mode/audit?limit=99999")
        # The endpoint clamps to 100.
        assert list_fn.call_args.kwargs["limit"] == 100


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_flip_accepted_returns_exit_0(capsys):
    accepted = FlipOutcome(
        accepted=True,
        strategy_name="some_strategy",
        target_mode="live",
        previous_mode="sandbox",
        new_mode="live",
        blockers=[],
        warnings=[],
        audit_id=1,
    )
    with patch(
        "services.strategy_mode_service.flip_mode",
        return_value=accepted,
    ):
        rc = _cli_main(["flip", "some_strategy", "live"])
    captured = capsys.readouterr()
    assert rc == 0
    assert '"accepted": true' in captured.out
    assert '"new_mode": "live"' in captured.out


def test_cli_flip_blocked_returns_exit_1(capsys):
    blocked = FlipOutcome(
        accepted=False,
        strategy_name="some_strategy",
        target_mode="live",
        previous_mode="sandbox",
        new_mode="sandbox",
        blockers=["broker down"],
        warnings=[],
        audit_id=2,
    )
    with patch(
        "services.strategy_mode_service.flip_mode",
        return_value=blocked,
    ):
        rc = _cli_main(["flip", "some_strategy", "live"])
    captured = capsys.readouterr()
    assert rc == 1
    assert '"accepted": false' in captured.out
    assert "broker down" in captured.out


def test_cli_flip_rejects_invalid_mode():
    with pytest.raises(SystemExit) as exc:
        _cli_main(["flip", "some_strategy", "yolo"])
    # argparse exits with code 2 for invalid choice
    assert exc.value.code == 2


def test_cli_audit_lists_rows(capsys):
    fake_rows = [{"id": 1, "accepted": True}]
    with patch(
        "database.strategy_mode_audit_db.list_attempts",
        return_value=fake_rows,
    ):
        rc = _cli_main(["audit", "some_strategy", "--limit", "5"])
    captured = capsys.readouterr()
    assert rc == 0
    assert '"id": 1' in captured.out


def test_cli_list_modes(capsys):
    fake_modes = [{"strategy_name": "x", "mode": "live"}]
    with patch(
        "database.strategy_mode_db.list_modes",
        return_value=fake_modes,
    ):
        rc = _cli_main(["list"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "live" in captured.out
