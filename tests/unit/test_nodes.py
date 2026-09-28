# CMN-C1-596 — Unit tests: node execute() logic with fake clients (no HTTP/secrets).

import json

from src.nodes.charge_verify_node import ChargeVerifyNode
from src.nodes.invoice_check_node import InvoiceCheckNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.refund_calc_node import RefundCalcNode
from src.nodes.refund_execute_node import RefundExecuteNode

JPY_CHARGE = {
    "found": True,
    "amount": 1000,
    "amount_refunded": 0,
    "currency": "jpy",
    "refunded": False,
    "status": "succeeded",
    "qualified_invoice_issued": True,
}


class FakeStripe:
    def __init__(self, charge=None, missing=False, raise_on_refund=False, has_credential=True):
        self._charge = charge if charge is not None else JPY_CHARGE
        self._missing = missing
        self._raise = raise_on_refund
        self._has_credential = has_credential
        self.refund_calls = []

    def credential_available(self):
        return self._has_credential

    def get_charge(self, cid):
        if self._missing:
            from src.services.stripe_client import StripeClientError

            raise StripeClientError("charge not found (404).")
        return dict(self._charge)

    def create_refund(self, idempotency_key, charge_id="", payment_intent="", amount=None, reason=None):
        if self._raise:
            from src.services.stripe_client import StripeClientError

            raise StripeClientError("refund boom")
        self.refund_calls.append((idempotency_key, charge_id, amount, reason))
        return {"refund_id": "re_1", "amount": amount or 1000, "currency": "jpy", "status": "succeeded"}


# ── PreProcessNode ──────────────────────────────────────────────────────────
def test_pre_process_parses_from_input_context():
    n = PreProcessNode()
    out = n.execute({"input_context": {"instruction": "refund charge ch_1234abcd in full, I confirm"}})
    intent = json.loads(out["parsed_intent"])
    assert intent["charge_id"] == "ch_1234abcd" and intent["confirm"] is True
    assert out["validation_error"] == "" and out["output_language"] == "en"


def test_pre_process_unparseable_is_graceful():
    n = PreProcessNode()
    out = n.execute({"input_context": {"instruction": "please refund the customer"}})
    assert out["validation_error"] and str(out["status"]).endswith("success")


def test_pre_process_s2_unsafe_rejected():
    n = PreProcessNode()
    state = {"input_context": {"instruction": "refund ch_1 ../../etc/passwd"}}
    n._extra_security_gate_input(state)
    out = n.execute(state)
    assert str(out["status"]).lower().endswith("error")


def test_pre_process_japanese_language():
    n = PreProcessNode()
    out = n.execute({"input_context": {"instruction": "charge ch_jp0001aa を全額返金 確認します"}})
    assert out["output_language"] == "ja"


# ── ChargeVerifyNode ──────────────────────────────────────────────────────────
def test_charge_verify_populates_facts():
    n = ChargeVerifyNode(client=FakeStripe())
    out = n.execute({"target_context": json.dumps({"charge_id": "ch_1"})})
    assert json.loads(out["charge_facts"])["currency"] == "jpy"


def test_charge_verify_not_found_graceful():
    n = ChargeVerifyNode(client=FakeStripe(missing=True))
    out = n.execute({"target_context": json.dumps({"charge_id": "ch_x"})})
    assert out["validation_error"] and str(out["status"]).endswith("success")


def test_charge_verify_short_circuits_on_error():
    n = ChargeVerifyNode(client=FakeStripe())
    out = n.execute({"status": "error", "validation_error": "boom"})
    assert out["charge_facts"] == ""


# ── RefundCalcNode ────────────────────────────────────────────────────────────
def test_refund_calc_full_from_remaining():
    n = RefundCalcNode()
    out = n.execute(
        {"parsed_intent": json.dumps({"amount": None, "reason": None}), "charge_facts": json.dumps(JPY_CHARGE)}
    )
    plan = json.loads(out["refund_plan"])
    assert plan["amount"] == 1000 and plan["is_full"] is True and plan["high_impact"] is True


def test_refund_calc_over_refund_blocked():
    n = RefundCalcNode()
    out = n.execute(
        {"parsed_intent": json.dumps({"amount": 5000, "reason": None}), "charge_facts": json.dumps(JPY_CHARGE)}
    )
    assert "exceeds" in out["validation_error"] and "refund_plan" not in out


def test_refund_calc_currency_mismatch_blocked():
    # "¥500" against a USD charge must refuse, not write 500 cents at the wrong scale.
    n = RefundCalcNode()
    facts = dict(JPY_CHARGE, currency="usd")
    out = n.execute(
        {"parsed_intent": json.dumps({"amount": 500, "amount_currency": "jpy"}), "charge_facts": json.dumps(facts)}
    )
    assert "does not match" in out["validation_error"] and "refund_plan" not in out


def test_refund_calc_matching_currency_allowed():
    n = RefundCalcNode()
    out = n.execute(
        {"parsed_intent": json.dumps({"amount": 400, "amount_currency": "jpy"}), "charge_facts": json.dumps(JPY_CHARGE)}
    )
    assert json.loads(out["refund_plan"])["amount"] == 400


def test_refund_calc_already_refunded_blocked():
    n = RefundCalcNode()
    facts = dict(JPY_CHARGE, amount_refunded=1000, refunded=True)
    out = n.execute({"parsed_intent": json.dumps({"amount": None}), "charge_facts": json.dumps(facts)})
    assert "already fully refunded" in out["validation_error"]


# ── InvoiceCheckNode ──────────────────────────────────────────────────────────
def test_invoice_check_jpy_qualified():
    n = InvoiceCheckNode()
    out = n.execute({"charge_facts": json.dumps(JPY_CHARGE)})
    assert out["invoice_notice"] == "qualified_required"


def test_invoice_check_jpy_flag_absent_conservative():
    n = InvoiceCheckNode()
    facts = dict(JPY_CHARGE, qualified_invoice_issued=None)
    out = n.execute({"charge_facts": json.dumps(facts)})
    assert out["invoice_notice"] == "conservative"


def test_invoice_check_non_jpy_no_notice():
    n = InvoiceCheckNode()
    facts = dict(JPY_CHARGE, currency="usd")
    out = n.execute({"charge_facts": json.dumps(facts)})
    assert out["invoice_notice"] == ""


# ── RefundExecuteNode (main) ──────────────────────────────────────────────────
def _exec_state(confirm=False, dry_run=False):
    return {
        "target_context": json.dumps({"charge_id": "ch_1", "confirm": confirm, "dry_run": dry_run, "case_ref": "CS-1"}),
        "refund_plan": json.dumps(
            {"amount": 1000, "currency": "jpy", "reason": "duplicate", "is_full": True, "high_impact": True}
        ),
    }


def test_execute_unconfirmed_needs_confirmation_no_write():
    c = FakeStripe()
    out = RefundExecuteNode(client=c).execute(_exec_state(confirm=False))
    assert json.loads(out["refund_result"])["needs_confirmation"] is True
    assert c.refund_calls == []


def test_execute_dry_run_no_write():
    c = FakeStripe()
    out = RefundExecuteNode(client=c).execute(_exec_state(dry_run=True))
    assert json.loads(out["refund_result"])["dry_run"] is True
    assert c.refund_calls == []


def test_execute_confirmed_writes_with_idempotency_key():
    c = FakeStripe()
    out = RefundExecuteNode(client=c).execute(_exec_state(confirm=True))
    res = json.loads(out["refund_result"])
    assert res["executed"] is True and res["refund_id"] == "re_1"
    assert len(c.refund_calls) == 1
    idem_key = c.refund_calls[0][0]
    assert idem_key.startswith("cmn-c1-596-")


def test_execute_idempotency_key_deterministic():
    c1, c2 = FakeStripe(), FakeStripe()
    RefundExecuteNode(client=c1).execute(_exec_state(confirm=True))
    RefundExecuteNode(client=c2).execute(_exec_state(confirm=True))
    assert c1.refund_calls[0][0] == c2.refund_calls[0][0]  # same logical refund -> same key


def test_execute_no_credential_fails_closed_no_write():
    # STG smoke: confirmed refund but no STRIPE_API_KEY provisioned → fail-closed,
    # no live POST, safe no_credential outcome at SUCCESS.
    c = FakeStripe(has_credential=False)
    out = RefundExecuteNode(client=c).execute(_exec_state(confirm=True))
    from framework.schemas.agent_status import AgentStatus

    res = json.loads(out["refund_result"])
    assert res["executed"] is False and res["no_credential"] is True
    assert c.refund_calls == []
    assert out["status"] == AgentStatus.SUCCESS.value


def test_charge_verify_no_credential_fails_closed_no_read():
    # charge_id present but no credential → no live GET, safe facts (found=False).
    c = FakeStripe(has_credential=False)
    out = ChargeVerifyNode(client=c).execute({"target_context": json.dumps({"charge_id": "ch_1"})})
    facts = json.loads(out["charge_facts"])
    assert facts["no_credential"] is True and facts["found"] is False


def test_execute_blocked_on_validation_error_no_write():
    c = FakeStripe()
    out = RefundExecuteNode(client=c).execute({"validation_error": "nope", "refund_plan": ""})
    assert json.loads(out["refund_result"])["executed"] is False
    assert c.refund_calls == []


def test_execute_api_error_surfaced_no_crash():
    c = FakeStripe(raise_on_refund=True)
    out = RefundExecuteNode(client=c).execute(_exec_state(confirm=True))
    assert json.loads(out["refund_result"])["executed"] is False
    assert out["validation_error"].startswith("Refund failed")


# ── PostProcessNode ───────────────────────────────────────────────────────────
def test_post_process_executed_report_en():
    n = PostProcessNode()
    state = {
        "output_language": "en",
        "refund_result": json.dumps(
            {"executed": True, "refund_id": "re_1", "charge_id": "ch_1", "amount": 1000, "currency": "jpy"}
        ),
        "invoice_notice": "qualified_required",
    }
    out = n.execute(state)
    assert "Executed" in out["formatted_output"] and "適格返還請求書" in out["formatted_output"]


def test_post_process_ja_has_disclaimer():
    n = PostProcessNode()
    state = {"output_language": "ja", "refund_result": json.dumps({"needs_confirmation": True, "charge_id": "ch_1"})}
    out = n.execute(state)
    assert "認証された翻訳ではありません" in out["formatted_output"]


def test_post_process_s3_redacts_stripe_key():
    n = PostProcessNode()
    leaked = {"formatted_output": ("refund done key=sk_live_" + "E" * 24), "confirmation_report": ""}
    scrubbed = n._extra_security_gate_output(leaked)
    assert ("sk_live_" + "E" * 24) not in scrubbed["formatted_output"]
    assert "[REDACTED-CREDENTIAL]" in scrubbed["formatted_output"]


# ── An empty result is not always a refusal ───────────────────────────────────
# Two different situations leave `refund_result` empty and only one is a refusal. The
# refund node is routed around when the caller cannot write on this channel, and that
# reader gets the notice naming the action that did not happen. But a confirmation
# prompt, a dry run or a failed precondition also leave the key empty, and each has its
# own report to render. Telling a caller who IS permitted that they are not permitted
# sends them looking for an access problem that does not exist.

#: A fragment of the shared notice that does not depend on this repo's action wording.
#: Asserting the whole sentence would copy the node's wording into the test, where it
#: drifts silently the first time someone rephrases it.
_REFUSAL_MARKER = "was not performed."


def _write_scope_state(trust, refund_result=""):
    return {"output_language": "en", "refund_result": refund_result, "caller_trust_level": trust}


def test_a_caller_who_cannot_write_gets_the_refusal_notice():
    out = PostProcessNode().execute(_write_scope_state("verified_external"))
    assert _REFUSAL_MARKER in out["formatted_output"]


def test_a_caller_who_CAN_write_is_never_told_they_cannot():
    """The half that was unpinned: with `internal` trust an empty result means something
    else happened, and the refusal notice would simply be false."""
    out = PostProcessNode().execute(_write_scope_state("internal"))
    assert _REFUSAL_MARKER not in out["formatted_output"]


def test_a_completed_refund_is_not_reported_as_refused():
    """The other direction, so the condition cannot collapse to its second half."""
    result = json.dumps(
        {"executed": True, "refund_id": "re_1", "charge_id": "ch_1", "amount": 1000, "currency": "jpy"}
    )
    out = PostProcessNode().execute(_write_scope_state("verified_external", refund_result=result))
    assert _REFUSAL_MARKER not in out["formatted_output"]
    assert "re_1" in out["formatted_output"]
