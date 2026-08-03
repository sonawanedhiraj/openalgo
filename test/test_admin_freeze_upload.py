"""End-to-end tests for ``POST /admin/api/freeze/upload`` (issue #540).

The route used to save the uploaded CSV to a hardcoded ``/tmp/qtyfreeze_upload.csv``.
On the Windows installs this project runs on there is no ``/tmp``: Python resolves
that path against the current drive (``C:\\tmp\\qtyfreeze_upload.csv``), so the
upload either raised ``FileNotFoundError`` or silently wrote outside any real temp
directory. Cleanup also sat on the happy path, leaking the file whenever the CSV
load raised.

These tests exercise the real Flask route through a test client — real multipart
upload, real ``FileStorage.save()``, real temp file, real CSV parse, real DB rows —
and pin the three properties the fix guarantees:

1. the temp file lands under the OS temp dir, never at a hardcoded ``/tmp`` path;
2. it is removed after a successful upload;
3. it is removed even when the CSV load raises (the ``finally`` block).

DB isolation is inherited from ``test/conftest.py`` (every ``DATABASE_URL`` is
redirected to a throwaway temp dir before any ``database.*`` import binds its
engine), so these tests can use the real ``qty_freeze_db`` session safely.
"""

from __future__ import annotations

import io
import os
import tempfile
from datetime import datetime

import pytest
import pytz
from flask import Flask

# The path the pre-fix code hardcoded, and where it actually lands on Windows.
_HARDCODED_TMP = "/tmp/qtyfreeze_upload.csv"
_HARDCODED_TMP_RESOLVED = os.path.abspath(_HARDCODED_TMP)

_CSV = "SYMBOL,VOL_FRZ_QTY\nNIFTY,1800\nBANKNIFTY,900\n"


@pytest.fixture
def admin_app():
    """A minimal Flask app with only ``admin_bp`` mounted."""
    from blueprints.admin import admin_bp
    from database import qty_freeze_db

    # conftest already repointed DATABASE_URL at a temp DB; just create the table.
    qty_freeze_db.init_db()
    qty_freeze_db.QtyFreeze.query.delete()
    qty_freeze_db.db_session.commit()

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.secret_key = "test-secret"  # pragma: allowlist secret
    app.register_blueprint(admin_bp)

    yield app

    qty_freeze_db.db_session.remove()


@pytest.fixture
def client(admin_app):
    """A test client carrying a valid (non-expired) session.

    ``check_session_validity`` REVOKES broker tokens and clears the session on the
    failure path, so the session is primed rather than the decorator patched out.
    """
    c = admin_app.test_client()
    now_ist = datetime.now(pytz.timezone("Asia/Kolkata"))
    with c.session_transaction() as sess:
        sess["logged_in"] = True
        sess["user"] = "testuser"
        sess["login_time"] = now_ist.isoformat()
    return c


def _upload(client, csv_text: str = _CSV, exchange: str = "NFO"):
    return client.post(
        "/admin/api/freeze/upload",
        data={
            "csv_file": (io.BytesIO(csv_text.encode()), "qtyfreeze.csv"),
            "exchange": exchange,
        },
        content_type="multipart/form-data",
    )


def test_upload_loads_rows_end_to_end(client):
    """The happy path: multipart CSV in, freeze-qty rows in the DB."""
    from database.qty_freeze_db import QtyFreeze

    resp = _upload(client)

    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["status"] == "success"
    assert body["count"] == 2

    rows = {r.symbol: r.freeze_qty for r in QtyFreeze.query.filter_by(exchange="NFO").all()}
    assert rows == {"NIFTY": 1800, "BANKNIFTY": 900}


def test_upload_never_writes_the_hardcoded_tmp_path(client, monkeypatch):
    """Regression for #540 — the CSV must land in the OS temp dir, not ``/tmp``.

    Asserted two ways so the test still fails on the pre-fix tree regardless of
    whether ``C:\\tmp`` happens to exist on the machine running it: the path handed
    to the loader must be under ``tempfile.gettempdir()``, and no file may appear
    at the hardcoded location.
    """
    from blueprints import admin

    seen: dict[str, str] = {}
    real_loader = admin.load_freeze_qty_from_csv

    def spy(csv_path, exchange="NFO"):
        seen["path"] = csv_path
        seen["existed_during_load"] = os.path.exists(csv_path)
        return real_loader(csv_path, exchange)

    monkeypatch.setattr(admin, "load_freeze_qty_from_csv", spy)

    hardcoded_pre_existed = os.path.exists(_HARDCODED_TMP_RESOLVED)

    resp = _upload(client)
    assert resp.status_code == 200, resp.get_data(as_text=True)

    used = seen["path"]
    # The upload really was written and readable while the loader ran...
    assert seen["existed_during_load"] is True
    # ...under the OS temp dir, on whatever platform this runs.
    assert os.path.normcase(os.path.abspath(used)).startswith(
        os.path.normcase(os.path.abspath(tempfile.gettempdir()))
    ), f"temp file escaped the OS temp dir: {used}"
    assert used.endswith(".csv")
    # ...and never at the old hardcoded path.
    assert os.path.normcase(os.path.abspath(used)) != os.path.normcase(_HARDCODED_TMP_RESOLVED)
    if not hardcoded_pre_existed:
        assert not os.path.exists(_HARDCODED_TMP_RESOLVED), (
            f"upload wrote to the hardcoded path {_HARDCODED_TMP_RESOLVED}"
        )
    # Cleanup ran on the success path.
    assert not os.path.exists(used), f"temp file leaked after a successful upload: {used}"


def test_temp_file_is_cleaned_up_when_the_load_raises(client, monkeypatch):
    """The cleanup lives in ``finally``, so a raising loader cannot leak the file."""
    from blueprints import admin

    seen: dict[str, str] = {}

    def boom(csv_path, exchange="NFO"):
        seen["path"] = csv_path
        raise RuntimeError("simulated CSV parse failure")

    monkeypatch.setattr(admin, "load_freeze_qty_from_csv", boom)

    resp = _upload(client)

    assert resp.status_code == 500
    assert resp.get_json()["status"] == "error"
    assert "path" in seen, "loader was never reached"
    assert not os.path.exists(seen["path"]), f"temp file leaked on the error path: {seen['path']}"


def test_concurrent_uploads_get_distinct_temp_paths(client, monkeypatch):
    """A fixed filename let two overlapping uploads clobber each other."""
    from blueprints import admin

    paths: list[str] = []
    real_loader = admin.load_freeze_qty_from_csv

    def spy(csv_path, exchange="NFO"):
        paths.append(csv_path)
        return real_loader(csv_path, exchange)

    monkeypatch.setattr(admin, "load_freeze_qty_from_csv", spy)

    assert _upload(client).status_code == 200
    assert _upload(client).status_code == 200

    assert len(paths) == 2
    assert paths[0] != paths[1], "two uploads reused the same temp path"


def test_non_csv_upload_is_rejected(client):
    """The extension guard still short-circuits before any temp file is created."""
    resp = client.post(
        "/admin/api/freeze/upload",
        data={"csv_file": (io.BytesIO(b"nope"), "payload.txt"), "exchange": "NFO"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert resp.get_json()["status"] == "error"
