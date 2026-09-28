"""AgentCore Platform v1.0 — PostProcessNode (CMN-C1-596).

Render an EN / JA / bilingual confirmation, needs-confirmation preview, dry-run preview,
or refusal report from the gate verdicts and refund result, append the Japan
qualified-invoice reminder when applicable, and enforce the S-3 output boundary (no
Stripe key value may cross into the report). Japanese output uses controlled
terminology (用語統制) and keigo, with a machine-translation disclaimer
(認証された翻訳ではありません). Charge ids and case references are preserved verbatim.
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.services.write_scope import caller_may_write
from shared.utils.audit_logger import emit_trace_event

# S-3: Stripe key shapes must never appear in the rendered report.
_KEY_PATTERNS = [
    re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{10,}\b"),  # sk_live_.. / rk_test_..
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"\b[A-Za-z0-9]{40,}\b"),  # long opaque tokens
]
_JA_DISCLAIMER = "※ 本要約は機械生成であり、認証された翻訳ではありません。"

# Japan qualified-invoice reminders (rendered from the invoice_notice code).
_INVOICE_EN = {
    "qualified_required": (
        "> **Japan qualified-invoice notice:** a qualified invoice (適格請求書) was issued "
        "for this transaction — you must issue a qualified refund invoice (適格返還請求書) "
        "for this refund. This is an operational reminder, not tax advice."
    ),
    "conservative": (
        "> **Japan qualified-invoice notice:** the qualified-invoice status could not be "
        "confirmed. If a qualified invoice (適格請求書) was issued, a qualified refund "
        "invoice (適格返還請求書) is required. This is an operational reminder, not tax advice."
    ),
}
_INVOICE_JA = {
    "qualified_required": (
        "> **インボイス制度に関するご注意：** 本取引には適格請求書が発行されています。"
        "本返金について適格返還請求書の発行が必要です。これは運用上の注意であり、税務助言ではありません。"
    ),
    "conservative": (
        "> **インボイス制度に関するご注意：** 適格請求書の発行状況を確認できませんでした。"
        "適格請求書が発行されている場合、適格返還請求書の発行が必要です。"
        "これは運用上の注意であり、税務助言ではありません。"
    ),
}


class PostProcessNode(FunctionNode):
    """Build the EN/JA refund confirmation/refusal report; S-3 output gate."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        language = state.get("output_language", "en") or "en"

        if not state.get("refund_result") and not caller_may_write(state):
            # The graph routed around the refund node: this caller cannot write on this
            # channel. Say which action did not happen, not "failed" -- a reader told the
            # generic sentence cannot tell a refusal from a bug, and will retry against a
            # charge they believe was never refunded.
            from src.services.write_scope import write_unavailable_notice  # noqa: PLC0415

            notice = write_unavailable_notice(
                language,
                action_en="The refund",
                action_ja="返金",
                channel_en="an authorised company billing system",
                channel_ja="社内の権限ある請求システム",
            )
            emit_trace_event("refund_report_refused", {"language": language}, state)
            return {
                "confirmation_report": notice,
                "formatted_output": notice,
                "status": AgentStatus.SUCCESS.value,
            }

        report_en = self._render_en(state)
        report_ja = self._render_ja(state)

        if language == "ja":
            report = report_ja
        elif language == "bilingual":
            report = f"{report_en}\n\n---\n\n{report_ja}"
        else:
            report = report_en

        emit_trace_event("report_compiled", {"language": language}, state)
        # get_output() surfaces `formatted_output` (or `result`) — write that so
        # invoke() returns the report; the outer graph does NOT override get_output.
        return {
            "confirmation_report": report,
            "formatted_output": report,
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_output(self, result: dict[str, Any]) -> dict[str, Any]:
        # S-3: redact any residual Stripe key value from all string outputs.
        for key in ("confirmation_report", "formatted_output"):
            val = result.get(key)
            if isinstance(val, str):
                for pat in _KEY_PATTERNS:
                    val = pat.sub("[REDACTED-CREDENTIAL]", val)
                result[key] = val
        return result

    # ── Outcome classification ────────────────────────────────────────────────
    @staticmethod
    def _outcome(state: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
        target = json.loads(state.get("target_context") or "{}")
        refund = json.loads(state.get("refund_result") or "{}")
        if refund.get("needs_confirmation"):
            kind = "needs_confirmation"
        elif refund.get("no_credential"):
            # No Stripe credential provisioned (e.g. STG smoke) — the agent
            # fail-closed and made no live call. A safe, non-error outcome.
            kind = "no_credential"
        elif state.get("validation_error"):
            kind = "error"
        elif refund.get("dry_run"):
            kind = "dry_run"
        elif refund.get("executed"):
            kind = "executed"
        else:
            kind = "error"
        return kind, target, refund

    @staticmethod
    def _target_label(target: dict[str, Any], refund: dict[str, Any]) -> str:
        return str(refund.get("charge_id") or target.get("charge_id") or target.get("payment_intent") or "?")

    def _invoice_block(self, state: dict[str, Any], lang: str) -> str:
        code = state.get("invoice_notice", "") or ""
        table = _INVOICE_JA if lang == "ja" else _INVOICE_EN
        text = table.get(code, "")
        return f"\n\n{text}" if text else ""

    def _render_en(self, state: dict[str, Any]) -> str:
        kind, target, refund = self._outcome(state)
        tid = self._target_label(target, refund)
        amt = refund.get("amount", "?")
        # The charge's own currency is authoritative and comes from Stripe. Without a
        # credential there is no charge to read, so fall back to the currency the caller
        # wrote -- otherwise the confirmation prompt reads "refund **5000 **" and the reader
        # is asked to approve an amount with no unit on it.
        intent = json.loads(state.get("parsed_intent") or "{}")
        cur = str(refund.get("currency") or intent.get("amount_currency") or "").upper()
        inv = self._invoice_block(state, "en")
        if kind == "needs_confirmation":
            return (
                f"# Refund — Confirmation Required\n\n**Target:** {tid}\n\n"
                f"You are about to refund **{amt} {cur}**. This is high-impact, "
                f"irreversible money movement. Re-issue the instruction with an "
                f"explicit confirmation to execute it.{inv}"
            )
        if kind == "no_credential":
            return (
                f"# Refund — Not Executed (no credential)\n\n**Target:** {tid}\n\n"
                f"No Stripe API credential is configured for this environment, so "
                f"no live Stripe call was made and no refund was created.{inv}"
            )
        if kind == "error":
            return (
                f"# Refund — Not Executed\n\n**Target:** {tid}\n\n"
                f"{state.get('validation_error', 'The request could not be processed.')}"
            )
        if kind == "dry_run":
            return (
                f"# Refund — Dry Run (no money moved)\n\n**Target:** {tid}\n\n"
                f"Would refund {amt} {cur}. No refund was created.{inv}"
            )
        return (
            f"# Refund — Executed\n\n**Target:** {tid}\n\n"
            f"Refunded {amt} {cur} (refund id {refund.get('refund_id', '?')}).{inv}"
        )

    def _render_ja(self, state: dict[str, Any]) -> str:
        kind, target, refund = self._outcome(state)
        tid = self._target_label(target, refund)
        amt = refund.get("amount", "?")
        cur = str(refund.get("currency", "")).upper()
        inv = self._invoice_block(state, "ja")
        if kind == "needs_confirmation":
            return (
                f"# 返金 — 確認が必要です\n\n**対象:** {tid}\n\n"
                f"**{amt} {cur}** を返金しようとしています。本返金は取り消しできない重要な金銭移動です。"
                f"実行するには、明示的な確認を付けて指示を再送してください。{inv}\n\n{_JA_DISCLAIMER}"
            )
        if kind == "no_credential":
            return (
                f"# 返金 — 未実行（認証情報なし）\n\n**対象:** {tid}\n\n"
                f"本環境には Stripe API の認証情報が設定されていないため、"
                f"実際の Stripe 呼び出しは行わず、返金も作成していません。{inv}\n\n{_JA_DISCLAIMER}"
            )
        if kind == "error":
            return f"# 返金 — 未実行\n\n**対象:** {tid}\n\nリクエストを処理できませんでした。\n\n{_JA_DISCLAIMER}"
        if kind == "dry_run":
            return (
                f"# 返金 — ドライラン（金銭移動なし）\n\n**対象:** {tid}\n\n"
                f"{amt} {cur} を返金予定です。返金は作成されていません。{inv}\n\n{_JA_DISCLAIMER}"
            )
        return (
            f"# 返金 — 実行完了\n\n**対象:** {tid}\n\n"
            f"{amt} {cur} を返金いたしました（返金ID {refund.get('refund_id', '?')}）。{inv}\n\n{_JA_DISCLAIMER}"
        )
