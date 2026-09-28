"""Two-way tests for write_scope.py. Copy verbatim; there is nothing to fill in."""

from __future__ import annotations

import pytest

from framework.schemas.trust_level import TrustLevel

from src.services.write_scope import (
    WRITE_TRUST,
    caller_may_write,
    caller_trust,
    write_unavailable_notice,
)


class TestTheComparisonFailsClosed:
    """A missing or unrecognised level must read as the LEAST privilege.

    Defaulting upward is how a typo in one manifest becomes an authorization.
    """

    @pytest.mark.parametrize(
        "state",
        [{}, {"caller_trust_level": None}, {"caller_trust_level": ""},
         {"caller_trust_level": "INTERNALS"}, {"caller_trust_level": "root"}, "not a dict"],
    )
    def test_unknown_is_anonymous(self, state):
        assert caller_trust(state) == "anonymous"
        assert caller_may_write(state) is False

    def test_the_marketplace_caller_cannot_write(self):
        """The whole point: VERIFIED_EXTERNAL is what the runner grants EVERY user."""
        assert caller_may_write({"caller_trust_level": "verified_external"}) is False
        assert caller_may_write({"caller_trust_level": TrustLevel.VERIFIED_EXTERNAL}) is False

    def test_an_internal_caller_can(self):
        """The other direction, or the check could refuse everything and still pass."""
        assert caller_may_write({"caller_trust_level": "internal"}) is True
        assert caller_may_write({"caller_trust_level": TrustLevel.INTERNAL}) is True

    def test_both_shapes_of_the_value_are_understood(self):
        """The framework puts a string in state; some call sites pass the enum."""
        for value in ("internal", "INTERNAL", TrustLevel.INTERNAL, str(TrustLevel.INTERNAL)):
            assert caller_trust({"caller_trust_level": value}) == "internal", value

    def test_the_required_level_is_internal_and_not_configurable_downward(self):
        assert WRITE_TRUST == "internal"
        # A repo passing a lower bar explicitly still gets a real comparison, not a pass.
        assert caller_may_write({"caller_trust_level": "anonymous"}, minimum="verified_external") is False
        assert caller_may_write({"caller_trust_level": "nonsense-level"}) is False


class TestTheReaderIsToldSomethingTrue:
    def test_the_notice_names_the_action_that_did_not_happen(self):
        notice = write_unavailable_notice("en", action_en="The admin role revocation", action_ja="管理者権限の削除")
        assert "The admin role revocation was not performed" in notice
        assert "管理者権限の削除" not in notice

    def test_an_unknown_language_gets_BOTH(self):
        notice = write_unavailable_notice("", action_en="The refund", action_ja="返金")
        assert "The refund was not performed" in notice
        assert "返金は実行していません" in notice

    def test_the_notice_names_no_internal_architecture(self):
        """A refusal must not teach the caller which gate to aim at next."""
        notice = write_unavailable_notice(None, action_en="The write", action_ja="書き込み")
        lowered = notice.lower()
        for leak in ("trust", "internal", "token", "verified_external", "s-1", "secret"):
            assert leak not in lowered, f"the notice names {leak!r}"
