"""AgentCore Platform v1.0 — InvoiceCheckNode (CMN-C1-596).

Japan qualified-invoice (インボイス制度) impact check. For a JPY charge, a refund of a
transaction for which a qualified invoice (適格請求書) was issued obliges the operator
to issue a qualified refund invoice (適格返還請求書). When the ``qualified_invoice_issued``
flag is absent we return a CONSERVATIVE reminder (per HuyVV7 design note). Non-JPY
charges get no notice. Read-only — this node NEVER blocks the write; it only annotates.

Output ``invoice_notice`` is a language-neutral CODE ("" | "qualified_required" |
"conservative"); PostProcessNode renders the EN/JA text.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


class InvoiceCheckNode(FunctionNode):
    """Set invoice_notice code for JPY charges (read-only, never blocks)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # Even on an upstream error we simply emit no notice (never blocks).
        if state.get("status") == AgentStatus.ERROR.value or state.get("validation_error"):
            emit_trace_event("invoice_check_skipped", {"reason": "upstream_error"}, state)
            return {"invoice_notice": "", "status": AgentStatus.SUCCESS.value}

        facts = json.loads(state.get("charge_facts") or "{}")
        currency = str(facts.get("currency", ""))

        if currency != "jpy":
            emit_trace_event("invoice_check_non_jpy", {"currency": currency}, state)
            return {"invoice_notice": "", "status": AgentStatus.SUCCESS.value}

        flag = facts.get("qualified_invoice_issued")
        if flag is True:
            code = "qualified_required"
        elif flag is None:
            code = "conservative"
        else:  # explicitly False — no qualified invoice was issued
            code = ""

        emit_trace_event("invoice_checked", {"code": code}, state)
        return {"invoice_notice": code, "status": AgentStatus.SUCCESS.value}
