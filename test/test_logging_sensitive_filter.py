"""Regression tests for `utils.logging.SensitiveDataFilter`.

The filter rewrites `record.args` in place to redact secrets. That is fine for
the usual positional-args case, but `logging.LogRecord.__init__` has a special
case: a SINGLE mapping argument is unwrapped, so `logger.info("%s", some_dict)`
leaves `record.args` as the dict itself (this is what enables `%(key)s`
formatting). Iterating a mapping yields its KEYS, so rebuilding `record.args`
as a tuple replaced the caller's data with its key names and made the later
`msg % record.args` raise:

    TypeError: not all arguments converted during string formatting

That turned a perfectly valid log call into a crash. It surfaced on CI (issue
#562) because pytest's LogCaptureHandler formats every record and propagates
the error, whereas the app's own ColoredFormatter swallows it in a fallback
branch — so locally the same call looked fine while silently logging nothing.

These tests pin the contract: the filter must never change the SHAPE of
`record.args`, only redact within it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

import pytest

from utils.logging import SensitiveDataFilter


@pytest.fixture
def filt():
    return SensitiveDataFilter()


def _record(msg: str, args: tuple):
    """Build a LogRecord the way `Logger._log` does — `args` is always a TUPLE.

    Passing a bare dict here instead would skip LogRecord's single-mapping
    unwrapping (and raise KeyError on a 1-key dict), so the tests would not
    exercise the code path that actually broke.
    """
    return logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1, msg=msg, args=args, exc_info=None
    )


def test_single_dict_arg_survives_filtering_and_formats():
    """The #562 crash: a dict arg must stay a dict and still render."""
    counts = {"checked": 1, "reconciled": 0, "rejected": 1, "pending": 0}
    record = _record("fill-reconcile: %s", (counts,))
    # LogRecord unwraps the single mapping — this is the precondition.
    assert record.args == counts

    assert SensitiveDataFilter().filter(record) is True

    assert isinstance(record.args, Mapping), "filter must not reshape a mapping into a tuple"
    # The real assertion: this used to raise TypeError.
    assert "checked" in record.getMessage()


def test_named_mapping_format_still_works(filt):
    """%(key)s formatting is the reason LogRecord unwraps mappings at all."""
    record = _record("order %(symbol)s qty %(qty)d", ({"symbol": "SBIN", "qty": 46},))
    assert filt.filter(record) is True
    assert record.getMessage() == "order SBIN qty 46"


def test_positional_args_are_unchanged(filt):
    """The ordinary path must keep working, types included."""
    record = _record("%s filled @ %.2f x%d", ("SBIN", 1087.05, 46))
    assert filt.filter(record) is True
    assert isinstance(record.args, tuple)
    assert record.getMessage() == "SBIN filled @ 1087.05 x46"


def test_numeric_args_keep_their_type(filt):
    """Redaction is a no-op here, so %d must not receive a str."""
    record = _record("count=%d", (5,))
    assert filt.filter(record) is True
    assert record.args == (5,)
    assert record.getMessage() == "count=5"


def test_secret_in_positional_arg_is_redacted(filt):
    """The filter's actual job still happens."""
    record = _record("payload=%s", ("apikey=supersecretvalue123",))
    assert filt.filter(record) is True
    assert "supersecretvalue123" not in record.getMessage()


def test_secret_in_mapping_value_is_redacted(filt):
    """Redaction must reach INSIDE a mapping, not be skipped for it."""
    record = _record("cfg %(apikey)s", ({"apikey": "supersecretvalue123"},))
    assert filt.filter(record) is True
    assert "supersecretvalue123" not in record.getMessage()


def test_empty_args_are_left_alone(filt):
    record = _record("no args here", None)
    assert filt.filter(record) is True
    assert record.getMessage() == "no args here"
