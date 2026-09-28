"""AgentCore Platform v1.0 — RefundExecuteNode (CMN-C1-596).

The ``main`` slot — the core capability and the single write. Executes one
``POST /v1/refunds`` via the Stripe client, but ONLY when the charge verified, the
amount computed cleanly, there is no upstream error, AND the caller explicitly
confirmed. A refund is high-impact, irreversible money movement, so an unconfirmed
request is withheld (``needs_confirmation``) and previewed; ``dry_run`` previews without
writing. The write carries a deterministic idempotency key so a retry of the same
logical refund never creates a second refund. A gate refusal / API failure is a valid
business outcome (status SUCCESS), not an execution error — post_process renders it.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.stripe_client import StripeClient, StripeClientError


class RefundExecuteNode(FunctionNode):
    """Guarded refund write — only after verify + calc + explicit confirmation."""

    # INTERNAL, and it stays INTERNAL. This node issues a real refund against the
    # customer's Stripe account. `run_agent_marketplace()` stamps every caller
    # VERIFIED_EXTERNAL with no surface to raise it, so declaring that level here makes
    # issuing refunds reachable by every Marketplace user -- the confirmation flag below
    # is a field in their own payload, not an authorisation. CoE ruled write modes out of
    # scope for this entry point and named the lowering as the prohibited workaround
    # (an internal ruling / the framework contract; (internal reference removed) rejects it as option one).
    #
    # The S-1 gate runs in BaseNode.__call__() BEFORE execute(), so this node cannot do
    # the check itself -- it would never run, and the whole invocation would end at status
    # error, which the runner raises on. Graph.add_edges() routes around it instead.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.INTERNAL

    def __init__(self, client: StripeClient | None = None) -> None:
        super().__init__()
        self._client = client or StripeClient()

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        blocked = self._blocked_reason(state)
        if blocked:
            emit_trace_event("refund_skipped", {"reason": blocked}, state)
            return {
                "refund_result": json.dumps(
                    {"executed": False, "dry_run": False, "needs_confirmation": False, "reason": blocked},
                    ensure_ascii=False,
                ),
                "status": AgentStatus.SUCCESS.value,
            }

        target = json.loads(state.get("target_context") or "{}")
        plan = json.loads(state.get("refund_plan") or "{}")
        charge_id = str(target.get("charge_id", ""))
        payment_intent = str(target.get("payment_intent", ""))
        case_ref = str(target.get("case_ref", ""))
        amount = plan.get("amount")  # int (minor units) or None = full
        currency = str(plan.get("currency", ""))
        reason = plan.get("reason")
        dry_run = bool(target.get("dry_run"))
        confirmed = bool(target.get("confirm"))

        base = {
            "charge_id": charge_id or payment_intent,
            "amount": amount if amount is not None else "full",
            "currency": currency,
        }

        # Confirmation gate — a refund always requires an explicit confirmation.
        if not confirmed and not dry_run:
            emit_trace_event("refund_needs_confirmation", {"target": base["charge_id"]}, state)
            return {
                "refund_result": json.dumps(
                    {"executed": False, "dry_run": False, "needs_confirmation": True, **base},
                    ensure_ascii=False,
                ),
                "status": AgentStatus.SUCCESS.value,
            }

        if dry_run:
            emit_trace_event("refund_dry_run", {"target": base["charge_id"]}, state)
            return {
                "refund_result": json.dumps(
                    {"executed": False, "dry_run": True, "needs_confirmation": False, **base},
                    ensure_ascii=False,
                ),
                "status": AgentStatus.SUCCESS.value,
            }

        # FAIL-CLOSED: a confirmed, non-dry-run refund is the single live write and
        # needs the Stripe credential. When no STRIPE_API_KEY is provisioned (e.g. the
        # STG smoke), never attempt a live POST — return a safe "cannot execute (no
        # credential)" outcome at SUCCESS so post_process renders a graceful notice,
        # instead of MissingSecret propagating to status=error. No refund is created.
        if not self._client.credential_available():
            emit_trace_event("refund_no_credential", {"target": base["charge_id"]}, state)
            return {
                "refund_result": json.dumps(
                    {"executed": False, "dry_run": False, "needs_confirmation": False, "no_credential": True, **base},
                    ensure_ascii=False,
                ),
                "status": AgentStatus.SUCCESS.value,
            }

        idem_key = self._idempotency_key(charge_id or payment_intent, amount, case_ref)
        try:
            result = self._client.create_refund(
                idempotency_key=idem_key,
                charge_id=charge_id,
                payment_intent=payment_intent,
                amount=amount,
                reason=reason,
            )
        except StripeClientError as exc:
            emit_trace_event("refund_error", {"reason": str(exc)}, state)
            return {
                "validation_error": f"Refund failed: {exc}",
                "refund_result": json.dumps(
                    {"executed": False, "dry_run": False, "needs_confirmation": False, "reason": str(exc), **base},
                    ensure_ascii=False,
                ),
                "status": AgentStatus.SUCCESS.value,
            }

        emit_trace_event(
            "refund_executed",
            {"refund_id": result.get("refund_id", ""), "target": base["charge_id"]},
            state,
        )
        return {
            "refund_result": json.dumps(
                {
                    "executed": True,
                    "dry_run": False,
                    "needs_confirmation": False,
                    "refund_id": result.get("refund_id", ""),
                    "charge_id": charge_id or payment_intent,
                    "amount": result.get("amount", amount if amount is not None else 0),
                    "currency": result.get("currency", currency),
                },
                ensure_ascii=False,
            ),
            "status": AgentStatus.SUCCESS.value,
        }

    @staticmethod
    def _idempotency_key(target: str, amount: int | None, case_ref: str) -> str:
        # With a case reference, the key is deterministic (target + amount + case),
        # so a retry of the SAME logical refund reuses the key and Stripe returns
        # the original refund instead of creating a duplicate.
        #
        # WITHOUT a case_ref there is no stable logical identity: two genuinely
        # distinct same-amount partial refunds of the same charge would otherwise
        # hash identically, and Stripe would silently drop the second while this
        # node still reported executed=True. So when case_ref is absent we mint a
        # fresh nonce — each parsed instruction is treated as a distinct intent
        # (refunds already require an explicit confirmation gate upstream, so this
        # cannot fire without operator intent).
        if case_ref:
            raw = f"{target}|{amount if amount is not None else 'full'}|{case_ref}"
            return "cmn-c1-596-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return "cmn-c1-596-" + uuid.uuid4().hex

    @staticmethod
    def _blocked_reason(state: dict[str, Any]) -> str:
        if state.get("status") == AgentStatus.ERROR.value:
            return "input rejected by security gate"
        if state.get("validation_error"):
            reason: str = str(state["validation_error"])
            return reason
        if not state.get("refund_plan"):
            return "no refund plan"
        return ""
