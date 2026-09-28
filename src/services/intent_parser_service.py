"""AgentCore Platform v1.0"""

# IntentParserService — CMN-C1-596
# Deterministic natural-language -> structured refund intent parsing.
# Handles English and Japanese instructions (NFKC-normalized). Stateless;
# unit-testable in isolation. Constructor-injected into PreProcessNode.
# MUST NOT be instantiated inside execute().

from __future__ import annotations

from typing import Any

import re
import unicodedata

# Stripe identifiers: charge ch_..., PaymentIntent pi_....
_CHARGE_RE = re.compile(r"\b(ch_[A-Za-z0-9]{6,})\b")
_PI_RE = re.compile(r"\b(pi_[A-Za-z0-9]{6,})\b")

# Case / ticket reference for the audit trail + idempotency key.
_CASE_RE = re.compile(r"(?:case|ticket|案件|ケース)\s*#?\s*([A-Za-z0-9][A-Za-z0-9\-_]{1,40})", re.I)

# Full-refund directive (amount omitted -> refund the full remaining balance).
_FULL_RE = re.compile(r"\b(full refund|refund in full|refund the (?:full|entire|whole))\b|全額", re.I)

# Partial amounts. NFKC folds full-width symbols/digits to ASCII first.
_JPY_SYMBOL_RE = re.compile(r"[¥￥]\s*([\d,]+)")
# NOTE: `円` is a word char, so a trailing `\b` fails whenever it is followed by
# kana/kanji (the normal case, e.g. "500円を返金") — which silently dropped the
# amount and escalated a partial refund to a FULL refund. Only the Latin
# `yen`/`jpy` forms need the word boundary.
_JPY_WORD_RE = re.compile(r"([\d,]+)\s*(?:円|(?:yen|jpy)\b)", re.I)
_USD_SYMBOL_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)")
_USD_WORD_RE = re.compile(r"([\d,]+(?:\.\d{1,2})?)\s*(?:usd|dollars?)\b", re.I)
# Bare "refund 500" (no currency) — interpreted as minor units.
_BARE_AMOUNT_RE = re.compile(r"\brefund(?:\s+of)?\s+([\d,]+)\b", re.I)

# Reason mapping to the Stripe enum {duplicate, fraudulent, requested_by_customer}.
_REASON_DUP_RE = re.compile(r"\b(duplicate|double[- ]charged?|double charge)\b|重複|二重", re.I)
_REASON_FRAUD_RE = re.compile(r"\b(fraud|fraudulent|unauthori[sz]ed)\b|詐欺|不正", re.I)
_REASON_CUST_RE = re.compile(
    r"\b(requested by customer|customer request|defective|broken|not as described|"
    r"changed (?:my|their) mind|wrong item)\b|顧客都合|返品|不良品|お客様都合",
    re.I,
)

# Explicit confirmation of the refund (a bare word is not enough — must be a directive).
_CONFIRM_RE = re.compile(
    r"\b(i confirm|confirmed|please confirm the refund|yes,? refund|go ahead|"
    r"approve the refund|execute the refund)\b"
    r"|確認します|確認済み|返金を承認|返金してください（確認）|はい、?返金",
    re.I,
)
# Dry-run must be an explicit directive — never a bare common verb (T-8).
_DRY_RUN_RE = re.compile(
    r"\b(dry[- ]?run|dryrun|what[- ]?if|no[- ]?refund|do ?n'?t refund|"
    r"without refunding|preview only|simulate the refund)\b"
    r"|ドライラン|返金せず|返金しない|シミュレーション",
    re.I,
)


class InputParseError(ValueError):
    """Raised when the instruction cannot be resolved to a charge/payment_intent."""


class IntentParserService:
    """Parse an NL refund instruction (EN or JA) into a structured intent.

    Returns ``{"charge_id", "payment_intent", "amount", "amount_currency", "reason",
    "case_ref", "confirm", "dry_run"}``. ``amount_currency`` is the currency implied by
    the amount marker ("usd" | "jpy" | None) — RefundCalcNode reconciles it against the
    verified charge currency and refuses a mismatch (never guesses the scale). Raises
    ``InputParseError`` when neither a charge id nor a payment_intent is present — the
    agent never refunds a target it cannot resolve.
    """

    def parse(self, text: str) -> dict[str, Any]:
        if not text or not text.strip():
            raise InputParseError("Empty instruction.")

        # NFKC folds full-width digits/latin/symbols (common in Japanese input) to ASCII.
        text = unicodedata.normalize("NFKC", text)

        charge_m = _CHARGE_RE.search(text)
        pi_m = _PI_RE.search(text)
        if not charge_m and not pi_m:
            raise InputParseError("No charge or payment_intent found — expected e.g. 'charge ch_12345'.")

        amount, amount_currency = self._parse_amount(text)
        return {
            "charge_id": charge_m.group(1) if charge_m else "",
            "payment_intent": pi_m.group(1) if pi_m else "",
            "amount": amount,
            "amount_currency": amount_currency,
            "reason": self._parse_reason(text),
            "case_ref": self._parse_case(text),
            "confirm": bool(_CONFIRM_RE.search(text)),
            "dry_run": bool(_DRY_RUN_RE.search(text)),
        }

    @staticmethod
    def _parse_case(text: str) -> str:
        m = _CASE_RE.search(text)
        return m.group(1) if m else ""

    @staticmethod
    def _parse_reason(text: str) -> str | None:
        if _REASON_DUP_RE.search(text):
            return "duplicate"
        if _REASON_FRAUD_RE.search(text):
            return "fraudulent"
        if _REASON_CUST_RE.search(text):
            return "requested_by_customer"
        return None

    @staticmethod
    def _parse_amount(text: str) -> tuple[int | None, str | None]:
        """Return ``(amount_in_minor_units, currency_marker)``.

        ``amount`` is None for a full refund. ``currency_marker`` is "usd"/"jpy" when the
        amount carried an explicit currency token, else None (bare number / full). The
        marker lets RefundCalcNode reject an instruction whose currency disagrees with the
        verified charge, instead of silently writing at the wrong scale. A full-refund
        directive wins over any incidental number. JPY is zero-decimal (the integer is the
        yen amount); USD amounts are converted to cents.
        """
        if _FULL_RE.search(text):
            return None, None

        m = _JPY_SYMBOL_RE.search(text) or _JPY_WORD_RE.search(text)
        if m:
            return int(m.group(1).replace(",", "")), "jpy"

        m = _USD_SYMBOL_RE.search(text) or _USD_WORD_RE.search(text)
        if m:
            return int(round(float(m.group(1).replace(",", "")) * 100)), "usd"

        m = _BARE_AMOUNT_RE.search(text)
        if m:
            return int(m.group(1).replace(",", "")), None

        # No amount stated and no explicit "full" -> default to a full refund.
        return None, None
