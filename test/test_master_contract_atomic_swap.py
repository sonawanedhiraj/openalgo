"""Issue #587 — the Zerodha master-contract SymToken swap must be atomic.

The pre-fix code committed the DELETE before the bulk INSERT started, leaving
the table empty for ~10s (112,919 rows) on every daily login while boot data
jobs were issuing token lookups. These tests pin the two properties that close
that window:

- the swap replaces old rows with new rows under ONE commit;
- a failed insert rolls back to the PREVIOUS contract (never an empty table)
  and propagates the failure.

The rollback test FAILS on the pre-fix tree, where the delete had already
committed by the time the insert raised.
"""

import pandas as pd
import pytest
from sqlalchemy.exc import IntegrityError

COLUMNS = [
    "symbol",
    "brsymbol",
    "name",
    "exchange",
    "brexchange",
    "token",
    "expiry",
    "strike",
    "lotsize",
    "instrumenttype",
    "tick_size",
]


def _row(symbol, token, **overrides):
    row = {
        "symbol": symbol,
        "brsymbol": symbol,
        "name": symbol,
        "exchange": "NSE",
        "brexchange": "NSE",
        "token": token,
        "expiry": "",
        "strike": -0.01,
        "lotsize": 1,
        "instrumenttype": "EQ",
        "tick_size": 0.05,
    }
    row.update(overrides)
    return row


@pytest.fixture()
def mcdb():
    import broker.zerodha.database.master_contract_db as mod

    mod.init_db()
    mod.SymToken.query.delete()
    mod.db_session.commit()
    yield mod
    mod.SymToken.query.delete()
    mod.db_session.commit()
    mod.db_session.remove()


def _seed_old_contract(mod):
    mod.db_session.bulk_insert_mappings(
        mod.SymToken, [_row("OLDONE", "111"), _row("OLDTWO", "222")]
    )
    mod.db_session.commit()


def _symbols(mod):
    return {r.symbol for r in mod.SymToken.query.all()}


def test_swap_replaces_old_contract_with_new(mcdb):
    _seed_old_contract(mcdb)
    new_df = pd.DataFrame(
        [_row("NEWONE", "333"), _row("NEWTWO", "444"), _row("NEWTHREE", "555")],
        columns=COLUMNS,
    )

    mcdb.replace_symtoken_table(new_df)

    assert _symbols(mcdb) == {"NEWONE", "NEWTWO", "NEWTHREE"}


def test_failed_insert_keeps_previous_contract(mcdb):
    """A mid-swap insert failure must leave the OLD rows in place.

    Two incoming rows share an explicit primary key, so the bulk insert raises
    IntegrityError after the (uncommitted) delete has run. Pre-fix, the delete
    had already committed and the table ended up empty.
    """
    _seed_old_contract(mcdb)
    bad_df = pd.DataFrame(
        [
            {**_row("DUPONE", "666"), "id": 99},
            {**_row("DUPTWO", "777"), "id": 99},
        ],
        columns=["id", *COLUMNS],
    )

    with pytest.raises(IntegrityError):
        mcdb.replace_symtoken_table(bad_df)

    assert _symbols(mcdb) == {"OLDONE", "OLDTWO"}


def test_empty_dataframe_still_clears_table_in_one_commit(mcdb):
    _seed_old_contract(mcdb)
    mcdb.replace_symtoken_table(pd.DataFrame([], columns=COLUMNS))
    assert _symbols(mcdb) == set()
