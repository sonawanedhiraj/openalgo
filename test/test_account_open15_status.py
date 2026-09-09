"""``/accounts`` open15 card: no broker call for a disabled / not-connected child (#719).

Before the fix ``open15_status`` read every child's broker book, including
``is_enabled=0`` children whose tokens were days old — two 403 tracebacks on
every 30 s poll of the accounts page.
"""

from __future__ import annotations

import services.account_open15_service as svc


def _account(i, *, enabled, last_login_at=None):
    return {
        "id": i,
        "display_name": f"child-{i}",
        "broker": "zerodha",
        "is_enabled": enabled,
        "last_login_at": last_login_at,
    }


def test_disabled_and_stale_children_never_hit_the_broker(monkeypatch):
    import services.broker_accounts_service as accounts

    reads = []
    monkeypatch.setattr(
        svc.broker_accounts_db,
        "list_accounts",
        lambda: [
            _account(1, enabled=False, last_login_at="2026-09-05 04:45:19"),  # disabled
            _account(2, enabled=True, last_login_at=None),  # enabled, never logged in today
        ],
    )
    monkeypatch.setattr(svc, "_todays_open15_trades", lambda aid, d: [])
    monkeypatch.setattr(svc, "_read_child_book", lambda b, t: reads.append(b) or [])
    monkeypatch.setattr(svc, "get_auth_token", lambda name: "key:stale-token")
    monkeypatch.setattr(accounts, "_is_connected", lambda a: False)

    out = svc.open15_status()

    assert reads == []
    by_id = {a["account_id"]: a for a in out["accounts"]}
    assert by_id[1]["enabled"] is False and by_id[1]["connected"] is False
    assert by_id[2]["enabled"] is True and by_id[2]["connected"] is False
    assert all(a["positions_readable"] is False for a in out["accounts"])


def test_disabled_child_is_not_connected_even_with_todays_login(monkeypatch):
    """A disabled child with a same-day token is still skipped — disabled wins."""
    import services.broker_accounts_service as accounts

    reads = []
    monkeypatch.setattr(
        svc.broker_accounts_db,
        "list_accounts",
        lambda: [_account(3, enabled=False, last_login_at="2026-09-09 02:49:38")],
    )
    monkeypatch.setattr(svc, "_todays_open15_trades", lambda aid, d: [])
    monkeypatch.setattr(svc, "_read_child_book", lambda b, t: reads.append(b) or [])
    monkeypatch.setattr(accounts, "_is_connected", lambda a: True)

    out = svc.open15_status()
    assert reads == []
    assert out["accounts"][0]["connected"] is False


def test_connected_child_book_is_read_once(monkeypatch):
    import services.broker_accounts_service as accounts

    reads = []
    monkeypatch.setattr(
        svc.broker_accounts_db,
        "list_accounts",
        lambda: [_account(3, enabled=True, last_login_at="2026-09-09 02:49:38")],
    )
    monkeypatch.setattr(svc, "_todays_open15_trades", lambda aid, d: [])
    monkeypatch.setattr(svc, "_read_child_book", lambda b, t: reads.append(b) or [])
    monkeypatch.setattr(svc, "get_auth_token", lambda name: "key:tok")
    monkeypatch.setattr(accounts, "_is_connected", lambda a: True)

    out = svc.open15_status()
    assert reads == ["zerodha"]
    row = out["accounts"][0]
    assert row["connected"] is True and row["positions_readable"] is True
