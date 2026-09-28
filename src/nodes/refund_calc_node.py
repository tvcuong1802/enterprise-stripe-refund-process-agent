"""AgentCore Platform v1.0 — RefundCalcNode (CMN-C1-596).

Compute the refund amount from the verified charge and the parsed intent. A refund of
more than the remaining refundable balance, or of an already fully-refunded charge, is
a graceful ``validation_error`` (SUCCESS) — no write. A refund is always high-impact
(irreversible money movement), so the plan always carries ``high_impact = True``.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


class RefundCalcNode(FunctionNode):
    """Compute refund_plan; block over-refund / already-refunded."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("status") == AgentStatus.ERROR.value or state.get("validation_error"):
            emit_trace_event("refund_calc_skipped", {"reason": "upstream_error"}, state)
            return {"refund_plan": "", "status": AgentStatus.SUCCESS.value}

        intent = json.loads(state.get("parsed_intent") or "{}")
        facts = json.loads(state.get("charge_facts") or "{}")
        requested = intent.get("amount")  # int (minor units) or None = full
        amount_currency = intent.get("amount_currency")  # "usd"|"jpy"|None
        reason = intent.get("reason")

        # Reconcile the instruction's currency marker with the verified charge currency —
        # refuse a mismatch instead of writing at the wrong minor-unit scale (e.g. "¥500"
        # against a USD charge). Only checked when both are known (charge is pre-verified).
        charge_currency = str(facts.get("currency", ""))
        if amount_currency and charge_currency and amount_currency != charge_currency:
            emit_trace_event(
                "refund_calc_currency_mismatch",
                {"instruction": amount_currency, "charge": charge_currency},
                state,
            )
            return {
                "validation_error": (
                    f"Instruction currency '{amount_currency}' does not match the charge "
                    f"currency '{charge_currency}'; refusing to guess the amount."
                ),
                "status": AgentStatus.SUCCESS.value,
            }

        # payment_intent path (charge not pre-verified): defer amount validation to
        # Stripe; a full refund unless an explicit amount was given.
        if facts.get("deferred"):
            plan: dict[str, Any] = {
                "amount": requested,  # may be None -> Stripe refunds the full amount
                "currency": "",
                "reason": reason,
                "is_full": requested is None,
                "high_impact": True,
            }
            emit_trace_event("refund_calc_deferred", {"is_full": plan["is_full"]}, state)
            return {"refund_plan": json.dumps(plan, ensure_ascii=False), "status": AgentStatus.SUCCESS.value}

        # No charge facts at all (should not happen after verify) — refuse safely.
        if not facts.get("found"):
            emit_trace_event("refund_calc_no_facts", {}, state)
            return {
                "validation_error": "Charge facts unavailable; cannot compute refund.",
                "status": AgentStatus.SUCCESS.value,
            }

        amount = int(facts.get("amount", 0))
        amount_refunded = int(facts.get("amount_refunded", 0))
        currency = str(facts.get("currency", ""))
        remaining = amount - amount_refunded

        if facts.get("refunded") or remaining <= 0:
            emit_trace_event("refund_calc_already_refunded", {"remaining": remaining}, state)
            return {
                "validation_error": "Charge is already fully refunded; nothing to refund.",
                "status": AgentStatus.SUCCESS.value,
            }

        if requested is None:
            refund_amount = remaining
            is_full = True
        else:
            requested = int(requested)
            if requested <= 0:
                emit_trace_event("refund_calc_invalid_amount", {"requested": requested}, state)
                return {"validation_error": "Refund amount must be positive.", "status": AgentStatus.SUCCESS.value}
            if requested > remaining:
                emit_trace_event("refund_calc_over_refund", {"requested": requested, "remaining": remaining}, state)
                return {
                    "validation_error": (
                        f"Refund amount {requested} exceeds the refundable remaining {remaining} ({currency})."
                    ),
                    "status": AgentStatus.SUCCESS.value,
                }
            refund_amount = requested
            is_full = requested == remaining

        plan = {
            "amount": refund_amount,
            "currency": currency,
            "reason": reason,
            "is_full": is_full,
            "high_impact": True,
        }
        emit_trace_event("refund_calculated", {"is_full": is_full, "currency": currency}, state)
        return {"refund_plan": json.dumps(plan, ensure_ascii=False), "status": AgentStatus.SUCCESS.value}
