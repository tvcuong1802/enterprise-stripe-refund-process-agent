"""AgentCore Platform v1.0"""

# ADR-005: State must be a flat TypedDict — never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects cause silent
# corruption. Extend AgentState with agent-specific fields only.
#
# SAFETY CONTRACT (CMN-C1-596):
# - All compound objects are stored as JSON-serialized str (never raw dict/list) so
#   the checkpoint stays msgpack-safe.
# - No Stripe key / credential in state (checkpoint DB leakage) — the key is fetched
#   at call time via current_secrets().require("STRIPE_API_KEY") and never persisted.
# - InvocationContext is accessed via config["configurable"] / InvocationContext
#   .from_state(state) only, never stored in state.

from __future__ import annotations

from typing import NotRequired

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Stripe refund state for CMN-C1-596.

    Shared fields (user_input, validated_input, status, session_id, node_history,
    error_log, formatted_output, result, input_context, caller_trust_level, hitl_*,
    etc.) are inherited from AgentState and are NOT redeclared here.

    Field ownership:
        PreProcessNode    -> parsed_intent, target_context, validation_error, output_language
        ChargeVerifyNode  -> charge_facts
        RefundCalcNode    -> refund_plan, (validation_error)
        InvoiceCheckNode  -> invoice_notice
        RefundExecuteNode -> refund_result, (validation_error)
        PostProcessNode   -> confirmation_report, formatted_output (inherited; surfaced by get_output)
    """

    # The language the model decided this reader wants, carried to the trailer.
    # Declared because LangGraph merges only declared fields -- an undeclared key is
    # dropped between nodes and the decision would be computed and lost.
    answer_language: str

    # ── PreProcessNode outputs ─────────────────────────────────────────────
    # JSON: {"charge_id": str, "payment_intent": str, "amount": int|null,
    #        "reason": str|null, "case_ref": str, "confirm": bool, "dry_run": bool}
    parsed_intent: NotRequired[str]  # default ""
    # JSON: {"charge_id": str, "payment_intent": str, "confirm": bool,
    #        "dry_run": bool, "case_ref": str}
    target_context: NotRequired[str]  # default ""
    # Non-empty string signals a business-invalid input; downstream nodes
    # short-circuit and post_process renders a refusal/error report.
    validation_error: NotRequired[str]  # default ""
    # Output rendering language: "en" | "ja" | "bilingual" (default "en").
    output_language: NotRequired[str]  # default "en"

    # ── ChargeVerifyNode output ────────────────────────────────────────────
    # JSON: {"found": bool, "amount": int, "amount_refunded": int, "currency": str,
    #        "refunded": bool, "status": str, "qualified_invoice_issued": bool|null}
    charge_facts: NotRequired[str]  # default ""

    # ── RefundCalcNode output ──────────────────────────────────────────────
    # JSON: {"amount": int, "currency": str, "reason": str|null,
    #        "is_full": bool, "high_impact": bool}
    refund_plan: NotRequired[str]  # default ""

    # ── InvoiceCheckNode output ────────────────────────────────────────────
    # Plain Japan qualified-invoice reminder (may be empty for non-JPY charges).
    invoice_notice: NotRequired[str]  # default ""

    # ── RefundExecuteNode (main slot) output ───────────────────────────────
    # JSON: {"executed": bool, "dry_run": bool, "needs_confirmation": bool,
    #        "refund_id": str, "charge_id": str, "amount": int, "currency": str}
    refund_result: NotRequired[str]  # default ""

    # ── PostProcessNode output ─────────────────────────────────────────────
    # Final Markdown confirmation/preview/refusal report. S-3 gate verifies no
    # Stripe key value crosses this boundary. `formatted_output` (inherited from
    # AgentState) carries the same report and is what get_output() surfaces.
    confirmation_report: NotRequired[str]  # default ""
