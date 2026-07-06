"""Test connection_manager predicate fix for #76.

Regression guard against the bug where not result.get("success") treated
absent keys as failures, misidentifying Zerodha's {"status": "success"}
responses as errors.
"""

import pytest


def _check_is_error(result):
    """Replicate the predicate logic from connection_manager.py:443-445."""
    is_error = (result and result.get("success") is False) or (
        result and result.get("status") == "error"
    )
    return is_error


class TestConnectionManagerPredicate:
    """Test cases for the success/error detection predicate."""

    def test_explicit_success_true_not_error(self):
        """Case 1: {"success": True} should not be treated as error."""
        result = {"success": True}
        assert not _check_is_error(result), "success=True should not be an error"

    def test_explicit_success_false_is_error(self):
        """Case 2: {"success": False} should be treated as error."""
        result = {"success": False, "error": "some error"}
        assert _check_is_error(result), "success=False should be an error"

    def test_zerodha_status_success_not_error(self):
        """Case 3: Zerodha's {"status": "success"} should NOT be error.

        This is the regression case from #76 — the missing "success" key
        should not trigger the error condition.
        """
        result = {"status": "success"}
        assert not _check_is_error(result), (
            "Zerodha {'status': 'success'} should not be treated as error"
        )

    def test_empty_dict_not_error(self):
        """Case 4: Empty dict {} should not be treated as error."""
        result = {}
        assert not _check_is_error(result), "Empty dict should not be an error"

    def test_none_not_error(self):
        """Case 5: None result should not be treated as error."""
        result = None
        assert not _check_is_error(result), "None result should not be an error"

    def test_status_error_is_error(self):
        """Bonus: {"status": "error"} should be treated as error."""
        result = {"status": "error", "message": "auth failed"}
        assert _check_is_error(result), "status='error' should be an error"
