# CMN-C1-596 — Unit tests: services (intent parser + Stripe client mappers/egress/verb+path).

import pytest

from src.services.intent_parser_service import InputParseError, IntentParserService
from src.services.stripe_client import StripeClient, StripeClientError


class TestIntentParserService:
    def setup_method(self):
        self.p = IntentParserService()

    def test_full_refund_charge(self):
        out = self.p.parse("Issue a full refund for charge ch_1234abcd — reason: defective product, case CS-2026-0892")
        assert out["charge_id"] == "ch_1234abcd"
        assert out["amount"] is None  # full
        assert out["reason"] == "requested_by_customer"
        assert out["case_ref"] == "CS-2026-0892"
        assert out["confirm"] is False and out["dry_run"] is False

    def test_partial_jpy_confirm_duplicate(self):
        out = self.p.parse("refund ¥500 for charge ch_9999zzzz, duplicate charge, I confirm")
        assert out["amount"] == 500 and out["reason"] == "duplicate" and out["confirm"] is True

    def test_usd_amount_to_cents(self):
        out = self.p.parse("refund $5.00 for charge ch_usd0001aa")
        assert out["amount"] == 500 and out["amount_currency"] == "usd"

    def test_jpy_amount_marks_currency(self):
        out = self.p.parse("refund ¥500 for charge ch_jp0001aa")
        assert out["amount"] == 500 and out["amount_currency"] == "jpy"

    def test_jpy_kanji_amount_followed_by_kana_is_partial(self):
        # Regression: `円` followed by kana/kanji (the normal JP phrasing) must
        # still parse the partial amount — previously the trailing \b failed and
        # the amount was dropped, silently escalating to a FULL refund.
        out = self.p.parse("charge ch_jp0002bb の500円を返金してください 確認します")
        assert out["amount"] == 500 and out["amount_currency"] == "jpy"
        assert out["charge_id"] == "ch_jp0002bb" and out["confirm"] is True

    def test_bare_amount_has_no_currency_marker(self):
        out = self.p.parse("refund 500 for charge ch_x00001aa")
        assert out["amount"] == 500 and out["amount_currency"] is None

    def test_dry_run_explicit_only(self):
        assert self.p.parse("dry run: refund charge ch_a00001aa in full")["dry_run"] is True
        # A bare "preview" must NOT trigger dry_run (would silently skip a real refund).
        assert self.p.parse("preview then refund charge ch_a00001aa in full")["dry_run"] is False

    def test_payment_intent_target(self):
        out = self.p.parse("refund pi_abc123def, fraud")
        assert out["payment_intent"] == "pi_abc123def" and out["reason"] == "fraudulent"

    def test_japanese_full_refund(self):
        out = self.p.parse("チケット 案件CS-9 の charge ch_jp0001aa を全額返金してください 確認します")
        assert out["charge_id"] == "ch_jp0001aa" and out["amount"] is None and out["confirm"] is True

    def test_nfkc_fullwidth_amount(self):
        # Full-width symbol/digits normalize to ASCII via NFKC.
        out = self.p.parse("refund ￥１２３ for charge ch_fw0001aa")
        assert out["amount"] == 123

    def test_missing_target_raises(self):
        with pytest.raises(InputParseError):
            self.p.parse("please refund the customer in full")

    def test_empty_raises(self):
        with pytest.raises(InputParseError):
            self.p.parse("   ")


class TestStripeClientMapping:
    def test_egress_guard_rejects_non_stripe(self):
        with pytest.raises(StripeClientError):
            StripeClient(base_url="https://evil.example.com")

    def test_egress_guard_allows_stripe(self):
        c = StripeClient()
        assert c._url("/v1/refunds") == "https://api.stripe.com/v1/refunds"

    def test_normalize_charge_jpy_qualified(self):
        out = StripeClient.normalize_charge(
            {
                "amount": 1000,
                "amount_refunded": 0,
                "currency": "JPY",
                "refunded": False,
                "status": "succeeded",
                "metadata": {"qualified_invoice_issued": "true"},
            }
        )
        assert out == {
            "found": True,
            "amount": 1000,
            "amount_refunded": 0,
            "currency": "jpy",
            "refunded": False,
            "status": "succeeded",
            "qualified_invoice_issued": True,
        }

    def test_normalize_charge_flag_absent_is_none(self):
        out = StripeClient.normalize_charge({"amount": 500, "currency": "usd", "metadata": {}})
        assert out["qualified_invoice_issued"] is None


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._payload = payload or {}

    def json(self):
        return self._payload


class TestStripeClientVerbPath:
    """N-17: assert the EXACT verb + path + idempotency header at the requests boundary."""

    def _bind(self, monkeypatch):
        # Bypass the secret provider — the header build calls current_secrets().require().
        import src.services.stripe_client as mod

        class _Secrets:
            def require(self, k):
                return ("sk_test_" + "E" * 24)

        monkeypatch.setattr(mod, "current_secrets", lambda: _Secrets())

    def test_get_charge_verb_and_path(self, monkeypatch):
        self._bind(monkeypatch)
        import src.services.stripe_client as mod

        seen = {}

        def fake_get(url, headers=None, timeout=None):
            seen["url"] = url
            seen["headers"] = headers
            return _Resp(200, {"amount": 1000, "currency": "jpy", "metadata": {}})

        monkeypatch.setattr(mod.requests, "get", fake_get)
        StripeClient().get_charge("ch_123")
        assert seen["url"] == "https://api.stripe.com/v1/charges/ch_123"

    def test_create_refund_verb_path_and_idempotency(self, monkeypatch):
        self._bind(monkeypatch)
        import src.services.stripe_client as mod

        seen = {}

        def fake_post(url, headers=None, data=None, timeout=None):
            seen["url"] = url
            seen["headers"] = headers
            seen["data"] = data
            return _Resp(200, {"id": "re_1", "amount": 1000, "currency": "jpy", "status": "succeeded"})

        monkeypatch.setattr(mod.requests, "post", fake_post)
        out = StripeClient().create_refund(
            idempotency_key="idem-key-1", charge_id="ch_123", amount=1000, reason="requested_by_customer"
        )
        assert seen["url"] == "https://api.stripe.com/v1/refunds"
        assert seen["headers"]["Idempotency-Key"] == "idem-key-1"
        assert seen["data"] == {"charge": "ch_123", "amount": 1000, "reason": "requested_by_customer"}
        assert out["refund_id"] == "re_1"

    def test_create_refund_requires_target(self, monkeypatch):
        self._bind(monkeypatch)
        with pytest.raises(StripeClientError):
            StripeClient().create_refund(idempotency_key="k")
