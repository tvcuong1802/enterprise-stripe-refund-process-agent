"""The liability trailer every answer carries — one mechanism, fleet-wide.

Rule (user, 2026-08-28): **every** output ends with a disclaimer. Not only the good
answers: a guidance message, a refusal, and an error all carry one too. An error is the
moment a reader is most likely to act on a half-answer, so it is the last place to drop
the notice.

Language follows the answer:

* answer written in English  -> English trailer
* answer written in Japanese -> Japanese trailer
* language undetermined, or the message is about bad/unreadable input -> **BOTH**

The bilingual case is not a fallback for laziness. When the agent could not read the
input, its language detection is at its least reliable, and guessing wrong hands the
reader a notice in a language they may not read on top of a request that already failed.

Keep this file byte-identical across repos (`shared-file-drift` watches it). What varies
per agent is the SCOPE line — what this particular output is and is not — passed in by
the caller. A generic "this is AI-generated" says nothing a reader can act on; the scope
line is where the agent states the decision it must not be used for.

    from src.services.disclaimer import disclaimer

    SCOPE_EN = ("Advisory only — audit evidence support, not a compliance guarantee and "
                "not a substitute for human attestation.")
    SCOPE_JA = "参考情報です。コンプライアンス保証ではなく、人手による確認に代わるものではありません。"

    text + disclaimer("en", scope_en=SCOPE_EN, scope_ja=SCOPE_JA)
    text + disclaimer(None, scope_en=SCOPE_EN, scope_ja=SCOPE_JA)   # bilingual
"""

from __future__ import annotations

# Marker every trailer carries, in both languages. Tests and the release gate look for
# this rather than for the wording, so the wording can be improved without breaking them.
MARKER_EN = "AI-generated"
MARKER_JA = "AI 生成"

_BASE_EN = "_AI-generated draft. Human review required before this is acted on._"
_BASE_JA = "_本内容は AI 生成のドラフトです。判断・実行の前に人手による確認を要します。_"

_SEPARATOR = "\n\n---\n\n"


def disclaimer(language: str | None, *, scope_en: str, scope_ja: str) -> str:
    """Return the trailer for ``language``; bilingual when it is unknown.

    ``language`` accepts "en"/"ja" (case and region suffixes are tolerated: "EN",
    "ja-JP"). Anything else -- including ``None``, an empty string, and a language this
    agent does not write in -- yields both, because a trailer the reader cannot read is
    the same as no trailer.
    """
    normalised = (language or "").strip().lower()[:2]
    english = f"{_BASE_EN}\n_{scope_en}_"
    japanese = f"{_BASE_JA}\n_{scope_ja}_"
    if normalised == "en":
        body = english
    elif normalised == "ja":
        body = japanese
    else:
        body = f"{english}\n\n{japanese}"
    return f"{_SEPARATOR}{body}\n"


def _prose(text: str) -> str:
    """The text a reader reads, with fenced blocks removed.

    A marker inside a fenced block is DATA, not a notice. (internal reference removed) ends its answer with
    a machine-readable JSON block carrying `"disclaimer": "AI-generated. Human review
    required before publication."`; a plain substring search found that, concluded the
    answer already carried a trailer, and skipped appending one. The reader was left with
    a JSON field where the liability notice should have been -- and every check agreed,
    because the string really was present (measured 2026-08-28).
    """
    import re  # noqa: PLC0415

    return re.sub(r"```.*?```", " ", text or "", flags=re.S)


def has_disclaimer(text: str) -> bool:
    """Does ``text`` carry a trailer in at least one language, in its PROSE?

    Used by the per-repo test and by anything that wants to assert the contract without
    depending on the exact sentence.
    """
    prose = _prose(text)
    return MARKER_EN in prose or MARKER_JA in prose


def is_bilingual(text: str) -> bool:
    """Does ``text`` carry BOTH language trailers, in its prose?"""
    prose = _prose(text)
    return MARKER_EN in prose and MARKER_JA in prose


# --- Language of the answer -------------------------------------------------
# MECHANISM lives here and is byte-identical in every repo (gate: shared-file-drift).
# POLICY is per agent: declare LANGUAGE_POLICY in src/services/agent_scope.py next to
# SCOPE_EN/SCOPE_JA. Nothing below is tuned for one agent.

DEFAULT_LANGUAGE_POLICY: dict[str, object] = {
    # A language counts as present only once it carries this many characters of real
    # content. Low on purpose: this picks which notice to print, and printing both is
    # the safe direction. Raise it for an agent whose answers routinely embed a short
    # quotation in the other language.
    "min_chars": 24,
    # When both languages clear the floor, the smaller one is treated as incidental if
    # the larger outweighs it by this factor. Lower = more willing to call an answer
    # bilingual. Set to 0 to disable and always report "both" when both are present.
    "dominance": 4,
    # Quoted machine output -- fenced blocks and inline code -- is prose in no language.
    # An incident report written in Japanese can quote more Latin log text than it
    # contains Japanese; counting that as English would mislabel the answer. Set False
    # only for an agent whose code fences carry human-language content.
    "ignore_quoted_code": True,
    # Regexes for this agent's own machine markup -- citation markers, tags, ids it emits
    # outside code fences. Same reasoning as ignore_quoted_code: markup is prose in no
    # language, and an agent that stamps one on every line would otherwise look bilingual.
    "ignore_patterns": (),
    # Latin tokens that are names, not English: standards, systems, product names.
    # ALL-CAPS acronyms and the agent id are excluded automatically -- list only
    # mixed-case names an agent keeps in Latin inside a Japanese answer.
    "identifiers": (),
}


def _repo_language_policy() -> dict[str, object]:
    """Per-agent overrides, if the agent declares any. Absent is a valid answer."""
    try:
        from src.services import agent_scope  # noqa: PLC0415
    except Exception:
        return {}
    policy = getattr(agent_scope, "LANGUAGE_POLICY", None)
    return dict(policy) if isinstance(policy, dict) else {}


def language_of_answer(text: str, *, policy: dict[str, object] | None = None) -> str | None:
    """ "en", "ja", or None for "both" -- read off the answer the reader receives.

    Not "does any Latin letter appear". A Japanese report legitimately carries Latin
    names -- a standard's abbreviation, the agent id -- and counting those as English
    made a fully translated Japanese document ask for a bilingual trailer (measured on
    (internal reference removed), 2026-08-28). What separates the two cases is whether each language
    carries SUBSTANCE: a bilingual message has a real paragraph in each, while a
    translated report has prose in one and a handful of names in the other.

    So Latin is counted as prose only -- words of four letters or more that are neither
    an ALL-CAPS acronym nor a declared name -- and each language must clear a floor.
    Thresholds come from DEFAULT_LANGUAGE_POLICY, overridden per agent by
    LANGUAGE_POLICY in agent_scope.py, overridden per call by `policy`.
    """
    import re  # noqa: PLC0415

    p: dict[str, object] = {
        **DEFAULT_LANGUAGE_POLICY,
        **_repo_language_policy(),
        **(policy or {}),
    }
    # Narrow by type rather than casting: a policy is hand-written per agent, and a wrong
    # type there should fall back to the default, not raise inside answer formatting.
    raw_floor, raw_dom = p.get("min_chars"), p.get("dominance")
    raw_names, raw_code = p.get("identifiers"), p.get("ignore_quoted_code")
    raw_pats = p.get("ignore_patterns")
    patterns = tuple(str(x) for x in raw_pats) if isinstance(raw_pats, (list, tuple)) else ()
    floor = raw_floor if isinstance(raw_floor, int) and not isinstance(raw_floor, bool) else 24
    dominance = float(raw_dom) if isinstance(raw_dom, (int, float)) and not isinstance(raw_dom, bool) else 4.0
    strip_code = raw_code if isinstance(raw_code, bool) else True
    names = {
        part.upper()
        for entry in (raw_names if isinstance(raw_names, (list, tuple, set)) else ())
        for part in re.split(r"[^A-Za-z]+", str(entry))
        if part
    }

    body = text or ""
    if strip_code:
        body = re.sub(r"```.*?```", " ", body, flags=re.S)
        body = re.sub(r"`[^`\n]*`", " ", body)
    for pattern in patterns:
        try:
            body = re.sub(pattern, " ", body, flags=re.S)
        except re.error:
            continue
    ja_chars = sum(1 for ch in body if "\u3040" <= ch <= "\u30ff" or "\u4e00" <= ch <= "\u9fff")
    latin_chars = sum(
        len(w) for w in re.findall(r"[A-Za-z][A-Za-z'-]{3,}", body) if not w.isupper() and w.upper() not in names
    )

    has_ja, has_en = ja_chars >= floor, latin_chars >= floor
    if has_ja and has_en:
        larger, smaller = max(ja_chars, latin_chars), min(ja_chars, latin_chars)
        if dominance > 0 and smaller * dominance < larger:
            return "ja" if ja_chars > latin_chars else "en"
        return None
    if has_ja:
        return "ja"
    if has_en:
        return "en"
    return None
