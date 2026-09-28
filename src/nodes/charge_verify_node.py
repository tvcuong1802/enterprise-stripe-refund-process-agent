"""AgentCore Platform v1.0 — ChargeVerifyNode (CMN-C1-596).

Verify the target charge exists and is refundable before any write: GET /v1/charges/{id}.
Read-only; short-circuits when an upstream error / validation_error is set. A missing
charge or a read error is a graceful ``validation_error`` (SUCCESS) — never a crash and
never a write.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.stripe_client import StripeClient, StripeClientError


class ChargeVerifyNode(FunctionNode):
    """Fetch + normalize the charge → charge_facts (JSON)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, client: StripeClient | None = None) -> None:
        super().__init__()
        self._client = client or StripeClient()

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("status") == AgentStatus.ERROR.value or state.get("validation_error"):
            emit_trace_event("charge_verify_skipped", {"reason": "upstream_error"}, state)
            return {"charge_facts": "", "status": AgentStatus.SUCCESS.value}

        target = json.loads(state.get("target_context") or "{}")
        charge_id = str(target.get("charge_id", ""))
        payment_intent = str(target.get("payment_intent", ""))

        # A refund can target a charge or a payment_intent. We can only pre-verify a
        # charge id; a payment_intent is verified by Stripe at refund time.
        if not charge_id:
            if payment_intent:
                emit_trace_event("charge_verify_pi_deferred", {"has_pi": True}, state)
                return {
                    "charge_facts": json.dumps(
                        {
                            "found": False,
                            "deferred": True,
                            "currency": "",
                            "amount": 0,
                            "amount_refunded": 0,
                            "refunded": False,
                            "status": "",
                            "qualified_invoice_issued": None,
                        },
                        ensure_ascii=False,
                    ),
                    "status": AgentStatus.SUCCESS.value,
                }
            emit_trace_event("charge_verify_no_target", {}, state)
            return {
                "validation_error": "No charge id to verify.",
                "status": AgentStatus.SUCCESS.value,
            }

        # FAIL-CLOSED: the charge read needs the Stripe credential. When no
        # STRIPE_API_KEY is provisioned (e.g. the STG smoke), never attempt a live
        # GET — return a safe "cannot verify" outcome at SUCCESS so the pipeline
        # completes and downstream fail-closes on the refund, instead of MissingSecret
        # propagating to status=error.
        if not self._client.credential_available():
            emit_trace_event("charge_verify_no_credential", {"charge_id": charge_id}, state)
            # deferred=True lets RefundCalcNode build a plan without live charge facts
            # (same shape as the payment_intent path); RefundExecuteNode then fail-closes
            # on the same credential probe and post_process renders the no_credential
            # notice — no live GET, no validation_error, a safe SUCCESS outcome.
            return {
                "charge_facts": json.dumps(
                    {
                        "found": False,
                        "deferred": True,
                        "no_credential": True,
                        "currency": "",
                        "amount": 0,
                        "amount_refunded": 0,
                        "refunded": False,
                        "status": "",
                        "qualified_invoice_issued": None,
                    },
                    ensure_ascii=False,
                ),
                "status": AgentStatus.SUCCESS.value,
            }

        try:
            facts = self._client.get_charge(charge_id)
        except StripeClientError as exc:
            emit_trace_event("charge_verify_error", {"reason": str(exc)}, state)
            return {
                "charge_facts": "",
                "validation_error": f"Could not verify charge: {exc}",
                "status": AgentStatus.SUCCESS.value,
            }

        emit_trace_event(
            "charge_verified",
            {"currency": facts["currency"], "refunded": facts["refunded"]},
            state,
        )
        return {
            "charge_facts": json.dumps(facts, ensure_ascii=False),
            "status": AgentStatus.SUCCESS.value,
        }
