"""What THIS agent's output must not be used for, and how it reads a request.

Kept in its own module so the wording lives beside the agent it describes, while the
MECHANISM stays byte-identical fleet-wide in ``disclaimer.py`` and ``input_intake.py``.
A generic "AI-generated draft" is true of every template here and tells a reader nothing
they can act on; this names the decisions the output must not stand in for.
"""

from __future__ import annotations

SCOPE_EN = "Reference only: a record of a Stripe refund request, the charge it resolved and the qualified-invoice impact it noted. It is not an accounting entry, not tax advice, and not confirmation that the refund settled. Verify in Stripe and with your finance team before relying on it."

SCOPE_JA = (
    " "
    "参考情報です。Stripe 返金リクエスト、対象取引の特定結果、およびインボイス制度上の影響に関する記録であり、会計仕訳でも、税務上の助言でも、返金完了の確認でもありません。ご利用前に Stripe および経理部門でご確認ください。"
)


# Names that stay in Latin script inside a Japanese answer -- they are names, not
# English prose, and a language check that counts them gets this agent wrong.
LANGUAGE_POLICY: dict[str, object] = {
    "identifiers": (
        "Stripe",
        "インボイス制度",
    ),
}


# What the intake call reads out of a free-form request. No `fields` are declared:
# this agent's own extractors already recover what it needs, and a second extractor
# would be a second source of truth. What the call adds is the language of the answer
# -- a request typed in romanised Japanese is entirely Latin, and reading the
# characters gets that reader wrong.
INTAKE_POLICY: dict[str, object] = {
    "languages": ("en", "ja"),
    "default_language": "en",
    "fields": {},
    "capabilities": (
        "Initiate a single Stripe refund from a plain-language instruction",
        "Resolve which charge the instruction refers to, and say so when it is ambiguous",
        "Note the qualified-invoice impact of the refund",
    ),
    "examples": (
        {
            "message": "Refund charge ch_3Abc for 5,000 JPY — the customer returned the item.",
            "expect": {"language": "en", "fields": {}, "fits": "yes", "suggestion": None},
        },
        {
            "message": "ch_3Abc の 5,000 円を返金してください。返品対応です。",
            "expect": {"language": "ja", "fields": {}, "fits": "yes", "suggestion": None},
        },
        {
            "message": "Close this customer's account entirely.",
            "expect": {"language": "en", "fields": {}, "fits": "no", "suggestion": 1},
        },
    ),
}

# Re-exported so every call site reads `from src.services.agent_scope import
# resolve_answer_language` -- the wording above is per agent, the mechanism is not, and it
# lives in language_decision.py where one patch fixes every repo.
from src.services.language_decision import (  # noqa: E402
    language_instruction,
    resolve_answer_language,
)

__all__ = [
    "INTAKE_POLICY",
    "LANGUAGE_POLICY",
    "SCOPE_EN",
    "SCOPE_JA",
    "language_instruction",
    "resolve_answer_language",
]
