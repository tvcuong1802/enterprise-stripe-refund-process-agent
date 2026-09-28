# CMN-C1-596 — Integration tests: full graph invoke with a mocked Stripe client.

from framework.schemas.invocation_context import InvocationContext, TrustLevel
from framework.secrets.context import NullProvider, bound_secrets

from src.graph.graph import Graph

JPY_QUALIFIED = {
    "found": True,
    "amount": 1000,
    "amount_refunded": 0,
    "currency": "jpy",
    "refunded": False,
    "status": "succeeded",
    "qualified_invoice_issued": True,
}
USD_CHARGE = {
    "found": True,
    "amount": 1000,
    "amount_refunded": 0,
    "currency": "usd",
    "refunded": False,
    "status": "succeeded",
    "qualified_invoice_issued": None,
}
FULLY_REFUNDED = {
    "found": True,
    "amount": 1000,
    "amount_refunded": 1000,
    "currency": "jpy",
    "refunded": True,
    "status": "succeeded",
    "qualified_invoice_issued": False,
}


class FakeClient:
    """No HTTP / no secrets — injected after compile()."""

    def __init__(self, charge=None, missing=False, raise_on_refund=False, has_credential=True):
        self._charge = charge if charge is not None else JPY_QUALIFIED
        self._missing = missing
        self._raise_on_refund = raise_on_refund
        self._has_credential = has_credential
        self.refund_calls = []

    def credential_available(self):
        return self._has_credential

    def get_charge(self, charge_id):
        if self._missing:
            from src.services.stripe_client import StripeClientError

            raise StripeClientError(f"charge '{charge_id}' not found (404).")
        return dict(self._charge)

    def create_refund(self, idempotency_key, charge_id="", payment_intent="", amount=None, reason=None):
        if self._raise_on_refund:
            from src.services.stripe_client import StripeClientError

            raise StripeClientError("refund boom")
        self.refund_calls.append((idempotency_key, charge_id, payment_intent, amount, reason))
        return {
            "refund_id": "re_1",
            "amount": amount or 1000,
            "currency": self._charge["currency"],
            "status": "succeeded",
        }


def _build(charge=None, missing=False, raise_on_refund=False):
    g = Graph()
    g.compile()
    client = FakeClient(charge=charge, missing=missing, raise_on_refund=raise_on_refund)
    g._nodes["charge_verify"]._client = client
    g._nodes["main"]._client = client
    return g, client


#: A caller who is actually permitted to write. The Marketplace runner grants
#: VERIFIED_EXTERNAL to every user and the refund node requires INTERNAL, so a write-path
#: test run at the default no longer exercises the refund at all -- it exercises the
#: refusal, which has its own test at the bottom of this file.
_WRITER = TrustLevel.INTERNAL


def _run(g, text, trust=_WRITER):
    ctx = InvocationContext(session_id="it", caller_trust_level=trust, caller_id="it")
    with bound_secrets(NullProvider()):
        return g.invoke(text, ctx=ctx, input_context={"instruction": text})


def test_unconfirmed_high_impact_requires_confirmation_no_write():
    g, client = _build()
    out = _run(g, "Issue a full refund for charge ch_1234abcd — reason: defective product, case CS-2026-0892")
    assert out["status"] == "success"
    assert "Confirmation Required" in out["output"]
    assert client.refund_calls == []


def test_confirmed_full_refund_writes():
    g, client = _build()
    out = _run(g, "Refund charge ch_1234abcd in full, defective, case CS-1, I confirm")
    assert out["status"] == "success"
    assert "Executed" in out["output"]
    assert len(client.refund_calls) == 1
    assert client.refund_calls[0][3] == 1000  # full remaining amount


def test_partial_refund_confirmed_writes_amount():
    g, client = _build()
    out = _run(g, "refund ¥400 for charge ch_1234abcd, duplicate, case CS-2, I confirm")
    assert "Executed" in out["output"]
    assert client.refund_calls[0][3] == 400


def test_dry_run_previews_without_writing():
    g, client = _build()
    out = _run(g, "dry run: refund charge ch_1234abcd in full")
    assert "Dry Run" in out["output"]
    assert client.refund_calls == []


def test_over_refund_blocked_no_write():
    g, client = _build(charge=USD_CHARGE)
    out = _run(g, "refund $50.00 for charge ch_usd0001aa, I confirm")
    assert out["status"] == "success"
    assert "Not Executed" in out["output"]
    assert client.refund_calls == []


def test_already_refunded_blocked_no_write():
    g, client = _build(charge=FULLY_REFUNDED)
    out = _run(g, "refund charge ch_done0001aa in full, I confirm")
    assert "Not Executed" in out["output"]
    assert client.refund_calls == []


def test_charge_not_found_no_write():
    g, client = _build(missing=True)
    out = _run(g, "refund charge ch_missing01aa in full, I confirm")
    assert out["status"] == "success"
    assert "Not Executed" in out["output"]
    assert client.refund_calls == []


def test_empty_input_graceful_no_write():
    g, client = _build()
    out = _run(g, "")
    assert out["status"] == "success"  # graceful, not error
    assert "Not Executed" in out["output"]
    assert client.refund_calls == []


def test_japanese_output_has_disclaimer_and_writes_on_confirm():
    g, client = _build()
    out = _run(g, "charge ch_jp0001aa を全額返金してください 案件CS-9 確認します")
    assert "認証された翻訳ではありません" in out["output"]
    assert len(client.refund_calls) == 1


def test_jpy_qualified_invoice_notice_present():
    g, _ = _build()
    out = _run(g, "refund charge ch_1234abcd in full, case CS-1, I confirm")
    assert "適格返還請求書" in out["output"]


def test_currency_mismatch_refused_no_write():
    # "¥500" against a USD charge must refuse rather than write 500 at the wrong scale.
    g, client = _build(charge=USD_CHARGE)
    out = _run(g, "refund ¥500 on charge ch_usd0001aa, I confirm")
    assert "Not Executed" in out["output"]
    assert "does not match" in out["output"]
    assert client.refund_calls == []


def test_needs_confirmation_shows_amount():
    # The one screen a human approves must show what is being refunded.
    g, client = _build(charge=USD_CHARGE)
    out = _run(g, "refund $4.00 on charge ch_usd0001aa")
    assert "Confirmation Required" in out["output"]
    assert "400" in out["output"]  # amount surfaced
    assert client.refund_calls == []


def test_under_trust_caller_refused_no_write():
    # S-1: an ANONYMOUS caller cannot move money → ERROR state, not a raise.
    g, client = _build()
    out = _run(g, "refund charge ch_1234abcd in full, I confirm", trust=TrustLevel.ANONYMOUS)
    assert str(out["status"]).lower().endswith("error")
    assert client.refund_calls == []


def test_refund_api_error_surfaced_not_crash():
    g, client = _build(raise_on_refund=True)
    out = _run(g, "refund charge ch_1234abcd in full, I confirm")
    # Write failure is surfaced as a not-executed report, not an unhandled crash.
    assert out["status"] == "success"
    assert "Not Executed" in out["output"]


def test_a_marketplace_caller_cannot_issue_a_refund():
    """The ruling, end to end.

    VERIFIED_EXTERNAL is exactly what `run_agent_marketplace()` stamps on every caller. If
    this run issued the refund, any Marketplace user could refund any charge -- the
    `confirm` flag is a field in their own payload, not an authorisation.

    Three things must hold together:
      - status SUCCESS, because the runner raises on anything else and the caller would be
        shown "agent failed" for a request that was merely not permitted
      - no Stripe call at all
      - a reply that names the action which did not happen, not a generic failure
    """
    g, client = _build(charge={"id": "ch_1234abcd", "amount": 5000, "currency": "usd", "refunded": False})
    out = _run(g, "refund charge ch_1234abcd in full, I confirm", trust=TrustLevel.VERIFIED_EXTERNAL)

    assert not client.refund_calls, f"a refund was attempted: {client.refund_calls}"
    assert out["status"] == "success"
    report = str(out.get("output") or out.get("formatted_output") or "")
    assert "was not performed" in report or "実行していません" in report, report[:300]
    for leak in ("TrustLevel", "VERIFIED_EXTERNAL", "INTERNAL", "token"):
        assert leak not in report, f"the refusal names {leak}"
