"""The reply envelope every agent returns. Byte-identical fleet-wide.

Two tiers, and mixing them up breaks `deploy-stg` while making the chat look better:

    get_output() returns  -> a DICT. `BaseGraph.invoke()` hands it straight to the
                             Marketplace runner (`result.get("status")`,
                             `result.get("output")`) and to
                             `scripts/stg_invoke_evidence.py`
                             (`body_json["status"] == "success"`). A bare str raises
                             AttributeError in one and fails agent_invoke_responsive in
                             the other.
    dict["output"]        -> a STR of Markdown. This is the only tier the chat reader sees.

What lives here is the part that does not vary: the trailer, the empty-message
short circuit, and the never-blank guarantee. What each repo supplies is its own
`rendered` payload and its own scope wording -- that is the half only a reader of the
agent can write.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: Shown when the message carries nothing the agent can work with. Bilingual on purpose:
#: this is the branch where the language is least knowable, and a notice the reader cannot
#: read is the same as no notice.
NOTHING_TO_WORK_WITH = (
    "There is nothing here to work on. Send the request in a sentence or two, saying what "
    "you want looked at.\n\n"
    "---\n\n"
    "お送りいただいた内容からは対応できる情報が読み取れませんでした。何について確認したいか、"
    "一〜二文でご記載ください。"
)

#: Shown when the pipeline finished but produced no payload at all. Without it a rejected
#: input returns `output=null` with `status=error`, which the platform shows as a blank
#: screen and "agent failed: RuntimeError".
NOTHING_PRODUCED = "No answer could be produced for this request.\nこの依頼に対する回答を生成できませんでした。"

#: Said in words when no written interpretation could be produced. A field named
#: `cannot_verify` is a schema key, not an explanation: the reader cannot tell a missing
#: interpretation from a rejected draft from a genuinely ambiguous record.
#:
#: Names NO internal machinery, and that is a rule, not a preference. The wording used to
#: be "no language model was available" -- true, unactionable, and it says more about how
#: this is built than about what the reader should do. The technical reason belongs in the
#: trace event, where an operator looks for it. Enforced by
#: test_no_user_facing_text_names_internal_architecture.
#:
#: Bilingual, because this branch is reached before any language decision would be made.
NO_MODEL_NOTICE = (
    "No written interpretation could be produced for this request, so the findings below "
    "are the mechanical checks alone. Have someone review them before acting.\n"
    "本リクエストでは説明文を作成できなかったため、以下は機械的な検出結果のみです。"
    "判断・実行の前に担当者による確認をお願いします。"
)

#: The same two notices in ONE language, used when the agent DID decide who it is
#: talking to. A reader who wrote in Japanese and gets a block of English with a
#: Japanese translation stapled underneath can see that the agent never worked out
#: which of the two it was for. The bilingual constants above stay for the case where
#: the message carried no letters at all and there is genuinely nothing to read.
NOTHING_TO_WORK_WITH_BY_LANGUAGE = {
    "en": ("There is nothing here to work on. Send the request in a sentence or two, saying what you want looked at."),
    "ja": (
        "お送りいただいた内容からは対応できる情報が読み取れませんでした。"
        "何について確認したいか、一〜二文でご記載ください。"
    ),
}

NOTHING_PRODUCED_BY_LANGUAGE = {
    "en": "No answer could be produced for this request.",
    "ja": "この依頼に対する回答を生成できませんでした。",
}


def _script_of(text: str) -> str | None:
    """What the characters say, or None when they say nothing.

    Two or more Latin letters in a row, or any kana/kanji. A string of punctuation and
    digits returns None -- it has no language, and answering it in "en" because it is
    non-empty is a guess wearing the clothes of a decision.
    """
    japanese = sum(1 for ch in text or "" if "\u3040" <= ch <= "\u30ff" or "\u4e00" <= ch <= "\u9fff")
    # Latin WORDS, not latin characters, and only things that look like words. A message
    # in Japanese that quotes an API key, a URL or a long id used to be read as English:
    # one 26-character token outweighed fourteen kana. A run longer than any real word is
    # an identifier, and an identifier says nothing about the language its sender writes
    # in. (No example literal here on purpose -- a credential-shaped string in a comment
    # is what the S-5 scanner is for, and it cannot tell a comment from code.)
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z']*", text or "") if 2 <= len(w) <= 20]
    latin = sum(len(w) for w in words)
    if japanese and japanese * 2 >= latin:
        return "ja"
    if latin:
        return "en"
    return None


#: Said to a caller the S-1 trust gate refused, and nothing else is said to them.
#: Bilingual because a refused caller never reached the node that decides a language.
#:
#: Two repo tests pulled in opposite directions here and both were right about something.
#: (internal reference removed) asserts the denied path leaks no output; (internal reference removed) asserts a refusal must
#: SAY something, because `output: None` is what the platform renders as a blank screen
#: under "agent failed" -- the reader is told nothing, not even that they were refused.
#: This is the line that satisfies both: it states the refusal, and it carries NO scope
#: line, so it reveals nothing about what the agent does to someone not allowed to use it.
REFUSED_NOTICE = "This request was refused.\nこのリクエストは拒否されました。"

_LETTERS = re.compile(r"[A-Za-z\u3040-\u309f\u30a0-\u30ff\u4e00-\u9faf]")

#: What the framework writes into `error_log` when IT refused the MESSAGE -- the S-2 input
#: gate or the S-3 output gate -- as opposed to the agent breaking. Markers, not an exact
#: string, because the wording differs by gate and by wheel version.
#:
#: `S-1 trust gate denied` is deliberately NOT here: that one is handled above by
#: `refused_before_answering`, and it is the one refusal that must reveal nothing at all.
#: `S-2`/`S-3` are matched as WHOLE tokens by regex, not as the substrings `"s-2 "` and
#: `"s-3 "`. Those two carried a trailing space and so missed the form a node writes most
#: often -- `"S-2: credential-shaped value detected in input_context"` -- which is a colon,
#: not a space. Measured 2026-09-15: that is (internal reference removed)'s own gate message, so the first
#: three repos patched by hand had the hole too, and it was invisible because the framework
#: path they were tested on says "cannot be processed safely" instead.
SECURITY_REFUSAL_MARKERS = (
    "cannot be processed safely",
    "security gate",
    "credential pattern",
)
#: S-2 ONLY, not S-3. An S-2 rejection is the framework declining to read the MESSAGE, and
#: the sender can fix it. An S-3 rejection is the agent having produced something its own
#: output gate would not pass -- an agent problem, which the reader can do nothing about and
#: must not be invited to retry. Measured 2026-09-15 on (internal reference removed), whose type-gate test
#: correctly objected when an egress failure started being reported as a refused message.
_GATE_TOKEN = re.compile(r"\bs-2\b", re.I)

#: Said to a LEGITIMATE user whose message the gate would not accept. It names what to
#: change, because that is the only thing the reader can act on -- and it names no gate, no
#: node and no rule id, because none of those help them. Bilingual: this branch is reached
#: before any language decision, on input the gate has already declined to read.
MESSAGE_REFUSED = (
    "## The request was not accepted\n\n"
    "The message carries something the input check will not take — usually an instruction "
    "addressed to the system, or a value shaped like a key, a token or a customer "
    "identifier. Send the question in your own words, without those, and it will go "
    "through.\n\n"
    "---\n\n"
    "## ご依頼を受け付けられませんでした\n\n"
    "入力チェックが受け付けない内容が含まれています。多くの場合、システムへの指示とみなされる"
    "文言か、キー・トークン・顧客識別情報の形をした文字列です。それらを除き、ご質問の内容のみ"
    "をお送りください。"
)


#: Credential shapes, checked on the text about to be SHOWN to the reader.
#:
#: The framework's own S-3 gate already does this, and for the shapes it knows it does it
#: better -- it sees every node result, not just the final answer. This is the half it
#: cannot cover: measured 2026-09-16 on wheel 1.0.3, its `sk-[a-zA-Z0-9]{20,}` stops at the
#: first hyphen, so `sk-proj-` (what OpenAI issues by default) and `sk-svcacct-` are not
#: detected at all, and two agents printed a caller's key back verbatim. The framework fix
#: belongs to CoE ((internal reference removed)); this is the layer templates own.
#:
#: `\b` keeps it off ordinary hyphenated prose, and the mandatory alnum first character
#: keeps it off an already-redacted value -- both were live mutants until a case was
#: written for each.
_CREDENTIAL_SHAPES = (
    re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_-]{19,}"),
    re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}"),
    re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bxox[abps]-[A-Za-z0-9-]{10,}"),
)

#: Said when the credential came from the SENDER. They can act on it, so they are told.
CREDENTIAL_IN_REQUEST_NOTICE = (
    "## The request was not answered\n\n"
    "It contains something shaped like an access token or credential, and this agent "
    "will not process or repeat one. Remove that value and send the request again -- this "
    "agent never needs it.\n\n"
    "---\n\n"
    "## ご依頼にお答えできませんでした\n\n"
    "アクセストークン等の認証情報らしい文字列が含まれています。本エージェントはそれを処理"
    "も再掲もしません。該当の値を削除して再度お送りください。本エージェントが認証情報を必要"
    "とすることはありません。"
)

#: Said when it did NOT come from the sender -- the agent produced it. Nothing the sender
#: can do, so they are not invited to retry, and the run stays an error for the operator.
CREDENTIAL_IN_OUTPUT_NOTICE = (
    "## This answer was withheld\n\n"
    "It contained something shaped like a credential that was not in the request, so it "
    "was not shown. This is an operations problem, not something to retry.\n\n"
    "---\n\n"
    "## 回答を差し止めました\n\n"
    "リクエストに含まれていなかった認証情報らしい文字列が出力に含まれていたため、表示して"
    "いません。再試行ではなく運用側の確認が必要です。"
)


def credential_in_text(text: Any) -> str:
    """The first credential-shaped run in `text`, or "" -- never the surrounding prose."""
    if not isinstance(text, str) or not text:
        return ""
    for pattern in _CREDENTIAL_SHAPES:
        m = pattern.search(text)
        if m:
            return m.group(0)
    return ""


def _came_from_the_sender(state: Any, secret: str) -> bool:
    """True when this exact value is in what the sender sent.

    The distinction decides WHO the reply is for, and they need opposite handling: a key
    the sender pasted is theirs to remove, and telling them so is useful. A key the agent
    produced is an operations problem the sender cannot act on, and inviting a retry there
    would loop them on a fault that is not theirs.
    """
    if not isinstance(state, dict) or not secret:
        return False
    for key in ("user_input", "raw_query", "validated_input", "normalized_query", "raw_free_text"):
        value = state.get(key)
        if isinstance(value, str) and secret in value:
            return True
    context = state.get("input_context")
    return isinstance(context, dict) and secret in str(context)


def _input_was_the_problem(state: Any) -> bool:
    """True when the run has POSITIVE evidence that the SENDER'S INPUT was the problem.

    Not "no evidence of a crash". That reading was wrong and a repo's own test proved it:
    (internal reference removed) fails a real question downstream and leaves `error_log` empty, so an
    absence-based rule dressed a genuine failure up as success. Absence is not evidence.

    What counts is a fact the agent recorded about the input: `validation_error` set, or a
    message that carries no letters at all. Both are statements about what was sent, and
    for both the composed sentence is the honest answer.
    """
    if not isinstance(state, dict):
        return False
    if str(state.get("validation_error") or "").strip():
        return True
    # A message that IS recorded and carries no letters -- punctuation, digits, a blank
    # line. That is a fact about what the sender sent.
    #
    # The ABSENCE of every input key is NOT that fact, and reading it as one was the
    # second version of this same mistake: many agents set `user_input = None` after
    # intake so the raw string is not re-persisted (N-9), so "no message in state" is
    # routine on a perfectly normal run. (internal reference removed)'s `test_a_run_that_actually_broke_
    # still_fails` is the case that shows the cost -- a node failure with no message left
    # in state would have been reported as success.
    for key in ("user_input", "raw_query", "validated_input", "normalized_query"):
        value = state.get(key)
        if isinstance(value, str) and value:
            return not _LETTERS.search(value)
    return False


#: An error_log entry that mentions one of these is talking about the INPUT, so it is not
#: evidence that the agent broke. Everything else that looks like a raised exception is.
_INPUT_FAULT_MARKERS = (
    "cannot be processed safely",
    "credential",
    "validation",
    "s-2",
    "pii",
)

#: What a raised exception leaves behind. `BaseNode.__call__` writes
#: `[{node_name}] {exc}\n{traceback}` into error_log, so a traceback or a bracketed node
#: name is a positive fact that some node's code raised.
_RAISED = re.compile(r"Traceback \(most recent call last\)|\[[A-Za-z_][A-Za-z_0-9]*Node\]")


def a_genuine_fault_was_recorded(state: Any) -> bool:
    """True when something in error_log says the AGENT raised, not that the input was bad.

    🔴 This exists because the status flip below was wrong without it, and the way it was
    wrong is the whole point of the round it came from.

    Measured on (internal reference removed), 2026-09-15: the S-3 egress gate rejected the handover and
    raised, `BaseNode.__call__` turned that into `status: error` plus a traceback in
    error_log, and `get_output` saw both. But the rejected payload meant nothing was in
    `formatted_output`, so `carries_nothing()` was true; and an EARLIER node had set
    `validation_error` (the request carried no records), so `_input_was_the_problem()` was
    also true. The flip then reported `status: success` and handed the reader the exact
    payload the security gate had just refused. Every test stayed green.

    `_input_was_the_problem()` answers "is there positive evidence the input was bad".
    That is necessary and NOT sufficient: both things can be true at once, and when they
    are, the failure is the one that must be reported. So this predicate is asymmetric on
    purpose -- a PRESENT fault marker is positive evidence of a fault, while an absent one
    proves nothing (which is why it is only ever used to BLOCK the flip, never to justify
    it; (internal reference removed) proved the absence reading wrong twice already).

    An entry naming an input gate is excluded: an S-2 refusal also raises in many repos,
    and that one SHOULD still be delivered as the sentence the sender can act on.
    """
    if not isinstance(state, dict):
        return False
    for entry in state.get("error_log") or ():
        text = str(entry)
        low = text.lower()
        if any(m in low for m in _INPUT_FAULT_MARKERS):
            continue  # this entry is about the input, not about the agent breaking
        if _RAISED.search(text):
            return True
    return False


def refused_the_message(state: Any) -> bool:
    """True when the framework refused the MESSAGE (S-2/S-3), not when the agent broke.

    The distinction decides who the reply is for. An agent that broke is an operations
    problem and `status: error` is the honest answer. A message the gate declined is a
    LEGITIMATE user holding something they can fix -- and they only find that out if the
    reply reaches them, which under `status: error` it never does:
    `normalize_terminal_output()` raises on any status but SUCCESS, so the runner discards
    the whole envelope and shows "agent failed".

    Measured across the fleet 2026-09-15 with a real model: 46 of the 49 repos probed
    returned `status: error` plus the generic "No answer could be produced for this
    request." on both the injection and the credential scenario. Every automated check
    agreed with it -- there WAS a body, it DID carry a trailer -- because the defect is in
    which sentence, and whether it is delivered at all.
    """
    if not isinstance(state, dict):
        return False
    if refused_before_answering(state):
        return False
    for entry in state.get("error_log") or ():
        text = str(entry).lower()
        if any(marker in text for marker in SECURITY_REFUSAL_MARKERS) or _GATE_TOKEN.search(text):
            return True
    return False


def refused_before_answering(state: Any) -> bool:
    """True when the S-1 trust gate refused the caller, so there is no answer to dress.

    Every OTHER failure gets a trailer and a sentence a reader can act on -- that is the
    whole rule. This one does not, and the difference is not politeness: a caller who is
    not permitted to invoke the agent must learn nothing about it, including what it is
    for. Manufacturing "there is nothing here to work on" plus a scope line hands an
    unauthorised caller a description of the agent and broke (internal reference removed)'s own
    fail-closed test, which asserts the denied path returns no output at all.

    Read from `error_log`, which `BaseNode.__call__` writes -- framework behaviour, the
    same in every repo, rather than a per-agent convention that would drift.
    """
    if not isinstance(state, dict):
        return False
    for entry in state.get("error_log") or ():
        if "S-1 trust gate denied" in str(entry):
            return True
    return False


def decided_language(state: Any) -> str | None:
    """The language the agent decided to answer in, or None if it never decided.

    ONLY `answer_language` counts, and that is the whole point of the function.

    `output_language` looks like the same thing and is not: it is a formatting field that
    repos default to a hardcoded literal when no caller declares one. Reading it here
    turns that default into a "decision" the agent never made -- measured on (internal reference removed),
    whose pre_process defaults it to "ja", so an English request came back with a
    Japanese-only trailer and every Marketplace reader got Japanese regardless of what
    they wrote. A caller's genuine declaration is not lost by ignoring it here:
    `resolve_answer_language()` already reads `output_language` FIRST and returns it as
    the decision, so `answer_language` is the value that accounts for both.

    Anything else -- an empty string, a locale like "ja-JP", a value the model invented --
    is not a decision either, and pretending otherwise puts the reader in a language
    nobody chose.
    """
    if not isinstance(state, dict):
        return None
    code = str(state.get("answer_language") or "").strip().lower()[:2]
    if code in ("en", "ja"):
        return code

    # No decision was made. That happens on the paths where S-2 rejected the input in
    # the gate, so `execute()` -- and the intake call inside it -- never ran. The MESSAGE
    # is still there and still readable, and choosing a language from its script is not
    # acting on refused content: it decides who is being spoken to, not what is said.
    # Without this the refusal path is bilingual for everyone, which is the one place a
    # reader is least able to spare the second half.
    for key in ("user_input", "raw_query", "validated_input", "normalized_query"):
        value = state.get(key)
        if isinstance(value, str) and value.strip():
            return _script_of(value)
    return None


_BARE_CODE = re.compile(r"^[a-z0-9]+(?:[_-][a-z0-9]+)*$")


def reads_as_prose(text: Any) -> bool:
    """True when this is a sentence a person can read -- not a code, not a serialised object.

    Three shapes turned up when the same branch was traced across the fleet, and only one
    of them belongs in `output`:

      (internal reference removed)  validation_error `missing_shipment_reference`  <- a machine code
                  payload "Please include a shipment reference (e.g. SHP123456)."
      (internal reference removed)  validation_error a full sentence
                  payload `{"status": "rejected", ...}`          <- a blob
      (internal reference removed)  validation_error a full sentence
                  payload `{'taxonomy_version': 'unversioned', ...}`  <- placeholders

    So neither slot is reliably the reader's sentence; the shape is what decides.
    """
    if not isinstance(text, str):
        return False
    body = text.strip()
    if not body or not _LETTERS.search(body):
        return False
    if body[0] in "{[":
        return False  # JSON or a dict repr, whatever it says inside
    if _BARE_CODE.match(body):
        return False  # `missing_shipment_reference` is for a log, not a reader
    return True


def carries_nothing(state: Any) -> bool:
    """True when the message has no letters and no kana at all.

    Judged on the message as received. Without it the pipeline runs on punctuation and
    produces the full report template with every field empty -- which reads as a broken
    agent, and costs the model calls to produce.
    """
    if not isinstance(state, dict):
        return False

    # A soft refusal counts as "nothing to work on" even when the message had letters in
    # it. Measured on (internal reference removed): an unreadable request set `validation_error` and the
    # pipeline still assembled a full report whose every field read "unidentified",
    # "unversioned", "unrouted" -- a page of placeholders where an explanation belonged.
    if str(state.get("validation_error") or "").strip():
        return True

    message = ""
    for key in ("user_input", "raw_query", "validated_input", "normalized_query"):
        value = state.get(key)
        if isinstance(value, str) and value:
            message = value
            break
    return not _LETTERS.search(message)


#: Keys that are provenance or plumbing, not something a reader wants a heading for.
_SKIP_KEYS = frozenset(
    {
        "trace_id",
        "correlation_id",
        "session_id",
        "node_history",
        "error_log",
        "status",
        "audit_log",
        "schema_version",
        "source_dates",
    }
)


def _humanise(key: str, labels: dict[str, str] | None) -> str:
    """A heading a reader can read. Never a field name.

    An unmapped key falling through to the raw identifier is how a report ends up with
    **online_in_person** as a heading -- the schema showing through the answer.
    """
    if labels and key in labels:
        return labels[key]
    return key.replace("_", " ").strip().capitalize()


def render_markdown(payload: Any, labels: dict[str, str] | None = None, _depth: int = 0) -> str:
    """A structured result as Markdown a person can read.

    The runner json.dumps() anything that is not a str, so a dict payload reaches the
    chat surface as braces, quoted keys and literal \n. Headings come from the keys,
    bullets from the lists; `labels` supplies the wording only where the humanised key
    would be wrong.
    """
    if payload is None or payload == "" or payload == [] or payload == {}:
        return ""
    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, (int, float, bool)):
        return str(payload)
    if isinstance(payload, list):
        parts = []
        for item in payload:
            rendered = render_markdown(item, labels, _depth + 1)
            if not rendered:
                continue
            # A nested block keeps its own structure; a scalar becomes one bullet.
            parts.append(
                "\n".join(f"- {line}" if i == 0 else f"  {line}" for i, line in enumerate(rendered.splitlines()))
            )
        return "\n".join(parts)
    if isinstance(payload, dict):
        parts = []
        for key, value in payload.items():
            if key in _SKIP_KEYS:
                continue
            rendered = render_markdown(value, labels, _depth + 1)
            if not rendered:
                continue
            heading = _humanise(str(key), labels)
            if _depth == 0:
                parts.append(f"## {heading}\n\n{rendered}")
            elif "\n" in rendered or isinstance(value, (dict, list)):
                parts.append(f"**{heading}**\n{rendered}")
            else:
                parts.append(f"**{heading}**: {rendered}")
        return "\n\n".join(parts)
    return str(payload)


def reader_markdown(value: Any) -> str:
    """Markdown a person can read, from whatever the node left in state.

    The trap this closes: `_render_payload` used to say

        if isinstance(payload, str):
            return payload.strip()

    and a node that stored `json.dumps(...)` satisfies that check exactly. The reader got
    `{"buckets": [{"bucket": "90d+", ...}]}` on screen, and every check passed -- it is a
    str, it is non-empty, the status is success. Seen on two repos before it was named.

    A string that PARSES as JSON is data that happens to be serialised, not prose. Render
    it. A string that does not parse is the agent's own text and is returned untouched.
    """
    if isinstance(value, str):
        body = value.strip()
        if not body:
            return ""
        if body[0] in "{[":
            try:
                return render_markdown(json.loads(body))
            except (ValueError, TypeError):
                return body  # opens like JSON, is not JSON -- the agent's own words
        return body
    return render_markdown(value)


def _with_sample_notice(state: Any, body: Any) -> Any:
    """`body` with the sample-data notice above it, when the answer rests on a sample.

    Applied on EVERY path that emits a body, not only the main one. Several agents null
    `user_input` after intake (N-9, so the raw caller string is not re-persisted), which
    makes `carries_nothing()` true at get_output time -- so those agents took the
    guidance path, which used to skip the notice entirely. Measured on (internal reference removed): a full
    policy-triage brief built on the bundled corpus went out with no notice at all, which
    is the one case the notice exists for.
    """
    if (
        not isinstance(body, str)
        or not body.strip()
        or not (isinstance(state, dict) and state.get("using_sample_data"))
    ):
        return body
    from src.services.sample_data import SAMPLE_DATA_NOTICE  # noqa: PLC0415

    if SAMPLE_DATA_NOTICE.splitlines()[0] in body:
        return body
    return SAMPLE_DATA_NOTICE + "\n\n---\n\n" + body.lstrip()


def _with_disclaimer_impl(
    envelope: dict[str, Any],
    state: Any,
    *,
    scope_en: str,
    scope_ja: str,
    rendered: str = "",
    preserve_as: str | None = None,
) -> dict[str, Any]:
    """Return the envelope with a reader-facing payload and the trailer at the very end.

    `rendered` is this agent's Markdown, when it has one; the framework payload is used
    otherwise. Every reply carries a trailer -- an answer, a piece of guidance, a refusal.
    The refusal path matters most: that is the one that used to come back blank.

    `preserve_as` keeps the STRUCTURED payload under its own key when prose replaces it in
    `output`. Without it, moving the report into `output` takes the data away from every
    other reader: the HTTP adapter, the domain boundary tests and anything downstream all
    index into it. Seven tests across two repos went from asserting on fields to indexing
    a string -- the trade-off is real, and this is the half that pays it back.
    """
    from src.services.disclaimer import disclaimer, has_disclaimer, language_of_answer

    if not isinstance(state, dict):
        state = {}
    original = envelope.get("output")
    # Fires whenever there IS a structured payload -- not only when a rendering was
    # produced. It used to require `rendered`, on the reasoning that without a rendering
    # `output` is not being replaced and so nothing is lost. That is wrong: when the
    # rendering comes back empty, `output` is replaced by the not-produced notice instead,
    # and the structured payload is gone from the envelope either way. Measured on an
    # agent whose ledger was empty: two integration tests went KeyError on the key that
    # exists precisely so they would not have to index a string.
    if preserve_as and original is not None:
        # A STRING payload is preserved too. The trailer is appended to `output`, so a
        # consumer that wants the agent's own text without it -- an adapter, a boundary
        # test, anything downstream -- has nowhere else to read it from. Skipping strings
        # left `result["formatted_output"]` missing on exactly the repos whose payload was
        # already prose, which is the half of the fleet this key exists for.
        envelope.setdefault(preserve_as, original)
    payload = rendered or original

    # A caller that explicitly asked for a machine format gets it unchanged. Such a body
    # carries its own `disclaimer` field, so appending prose after it would both duplicate
    # the notice and stop the document parsing. The Marketplace never sets `output_format`,
    # so the chat surface is unaffected -- this is for a programmatic caller.
    #
    # Lives here because it was found MISSING: it existed in one repo's private copy and
    # was dropped when that repo moved onto this module, turning a passing integration
    # test red. Consolidating copies has to carry every branch the copies had, and the
    # only way to know is to compare them rather than to replace them.
    if str(state.get("output_format") or "").strip().lower() == "json":
        return envelope

    # A node that decides the refusal ITSELF still has to be heard. Several repos render
    # their own rejection prose in post_process and set `refusal_kind` in state; the
    # framework envelope carries only its own keys, so that decision was computed and then
    # dropped at the boundary. Measured on (internal reference removed) 2026-09-15: post_process returned
    # `refusal_kind: "input"`, the reader got the right sentence, and the envelope still
    # reported `refusal_kind: None` -- so a consumer could not tell the refusal from an
    # answer, which is the entire reason the field exists.
    #
    # Copied only when the envelope has none: the branches below are more specific than a
    # node's own guess and must win.
    if not envelope.get("refusal_kind"):
        carried = state.get("refusal_kind") if isinstance(state, dict) else None
        if isinstance(carried, str) and carried.strip():
            envelope["refusal_kind"] = carried

    if refused_before_answering(state):
        # The refusal, and nothing else -- no scope line, no guidance, no trailer.
        envelope["output"] = REFUSED_NOTICE
        envelope["refusal_kind"] = "trust"
        return envelope

    if refused_the_message(state):
        # SUCCESS, deliberately, on a path that failed. The runner raises on every other
        # status, so an `error` here throws away the sentence that tells a legitimate user
        # what to change -- and they resend the same message. `error_log` keeps the reason
        # and the audit trail is untouched; what changes is only whether the reader is
        # allowed to read the refusal that was already written for them.
        language = decided_language(state)
        envelope["status"] = "success"
        # The agent's OWN refusal wins when it has one. This branch exists to make a
        # refusal readable, not to flatten every domain's wording into one sentence:
        # "the shipment could not be classified" tells the sender which step stopped and
        # the generic line does not. Measured 2026-09-15 on (internal reference removed), whose own test
        # objected -- correctly -- when the shared wording replaced its specific one.
        # Same principle the `carries_nothing` branch below already applies.
        own = payload if reads_as_prose(payload) else ""
        if not own:
            candidate = state.get("validation_error")
            own = candidate if reads_as_prose(candidate) else ""
        # Say WHY, in a field, so nothing downstream has to infer a refusal from `status`.
        # Every consumer that needed to know -- an adapter, a boundary test, the evidence
        # script -- was reading `status == "error"`, which is precisely the thing that had
        # to change for the reader to see the refusal at all. A contract that can only be
        # read by the symptom it is fixing is not a contract.
        envelope["refusal_kind"] = "input"
        body = str(own).rstrip() if own else MESSAGE_REFUSED
        envelope["output"] = body + disclaimer(language, scope_en=scope_en, scope_ja=scope_ja)
        return envelope

    if carries_nothing(state):
        # ONE sentence, never two. This branch used to APPEND the generic message after
        # whatever the agent had already said, so a reader who sent a real request was
        # told both what was missing AND that they had sent nothing -- two sentences that
        # contradict each other, measured on seven repos. `status` was success and
        # `output` was non-empty throughout, which is why the fleet live-path assertion
        # could not see it: the defect is in WHICH sentence, not in whether there is one.
        #
        # Prefer the agent's own words when they read as prose: it says what is actually
        # missing, and the generic line does not. When the payload is a blob or a machine
        # code, `validation_error` usually carries the sentence instead -- and when
        # neither does, the generic line is all there is.
        spoken = payload if reads_as_prose(payload) else ""
        if not spoken:
            candidate = state.get("validation_error")
            spoken = candidate if reads_as_prose(candidate) else ""
        language = decided_language(state)
        # A deliberate trade: the agent's specific sentence may be English while the reader
        # wrote Japanese. Correct information in the wrong language beats a fluent sentence
        # that says something untrue about what they sent. The trailer still follows them.
        body = spoken or (NOTHING_TO_WORK_WITH_BY_LANGUAGE[language] if language else NOTHING_TO_WORK_WITH)
        body = _with_sample_notice(state, body)
        envelope["output"] = body.rstrip() + disclaimer(language, scope_en=scope_en, scope_ja=scope_ja)
        # This branch is defined by a POSITIVE fact about the sender's input -- either the
        # agent set `validation_error`, or the message carried no letters at all -- and it
        # composed the sentence that says so. Leaving `status: error` under it means the
        # runner raises and that sentence is never read, so the branch existed for nothing.
        # Measured 2026-09-15: ten repos answered an empty message with a good bilingual
        # "tell me what to look at" and the sender saw "agent failed".
        #
        # The condition is that positive fact, NOT the absence of a crash marker. An
        # earlier version read an empty `error_log` as "nothing broke", and (internal reference removed)'s
        # `test_a_genuine_error_is_not_dressed_up_as_success` disproved it with a real
        # question that failed and left `error_log` empty. Absence is not evidence.
        # AND no recorded fault. `_input_was_the_problem()` alone flipped a genuine S-3
        # egress refusal into a success on (internal reference removed): the gate rejected the payload, so
        # nothing reached `formatted_output` and this branch was entered, while an earlier
        # node's `validation_error` made the input look like the cause. Two true facts,
        # and the flip picked the wrong one. See `a_genuine_fault_was_recorded`.
        if (
            str(envelope.get("status")) == "error"
            and _input_was_the_problem(state)
            and not a_genuine_fault_was_recorded(state)
        ):
            envelope["status"] = "success"
        return envelope

    # An answer built on bundled sample data says so, ABOVE the answer, before anyone
    # reads a number out of it. The whole point of shipping samples is that the agent can
    # be tried; the risk is that a demonstration gets mistaken for a result, and a notice
    # printed after two pages of findings is a notice nobody reaches.
    payload = _with_sample_notice(state, payload)

    if isinstance(payload, str) and has_disclaimer(payload):
        envelope["output"] = payload
        return envelope

    if not isinstance(payload, str) or not payload.strip():
        language = decided_language(state)
        envelope["output"] = (NOTHING_PRODUCED_BY_LANGUAGE[language] if language else NOTHING_PRODUCED) + disclaimer(
            language, scope_en=scope_en, scope_ja=scope_ja
        )
        # DELIBERATELY no status change here. This branch fires when the pipeline produced
        # no payload at all, and that is the same shape a genuine downstream failure has --
        # so there is no evidence in it about whose fault it is. Flipping it to success made
        # (internal reference removed)'s `test_a_genuine_error_is_not_dressed_up_as_success` fail on a real
        # question that failed with an EMPTY `error_log`, which is the case that disproves
        # "absent error_log means nothing broke". A repo whose input refusal lands here
        # should RECORD the refusal (`validation_error`, or a marker in `error_log`) rather
        # than have this layer guess on its behalf.
        return envelope

    # The agent's own language decision wins when it made one. `language_of_answer` reads
    # the PAYLOAD, which for a structured report is mostly field labels and identifiers --
    # and for a request typed in romanised Japanese it is entirely Latin, so it answers a
    # Japanese reader in English. A decision the agent computed and then did not use is
    # the same as no decision at all.
    decided = decided_language(state) or ""
    language = decided if decided in ("en", "ja") else language_of_answer(payload)
    envelope["output"] = payload.rstrip() + disclaimer(language, scope_en=scope_en, scope_ja=scope_ja)
    return envelope


def with_disclaimer(
    envelope: dict[str, Any],
    state: Any,
    *,
    scope_en: str,
    scope_ja: str,
    rendered: Any = None,
    preserve_as: str | None = None,
) -> dict[str, Any]:
    """The envelope, plus one last look at what is about to be shown.

    A WRAPPER, not a seventh branch. `_with_disclaimer_impl` has seven return points --
    trust denial, message refusal, machine format, carries-nothing, sample notice, already
    has a trailer, nothing produced -- and a check added to one of them is a check the
    other six do not have. Every defect in this round's history is that shape: a property
    asserted at the layer being edited, silent about the layer beside it. So the outgoing
    text is derived ONCE, here, after the answer exists and before any caller sees it.

    What it adds: a credential-shaped value in the text a reader would receive is never
    shown. The framework's S-3 gate already does this for the shapes it knows, and does it
    better -- it sees every node result, not just the final answer. Measured 2026-09-16 on
    wheel 1.0.3, the shapes it knows do not include `sk-proj-` or `sk-svcacct-`, and two
    agents printed a caller's key back verbatim. The framework fix is CoE's
    ((internal reference removed)); this is the layer templates own, and it is deliberately narrow:
    it changes nothing for an answer that carries no credential.
    """
    # Imported inside the function, the way the implementation below does it: this module
    # is copied into repos whose import graph differs, and a module-level import here
    # would change when it is resolved.
    from src.services.disclaimer import disclaimer  # noqa: PLC0415

    envelope = _with_disclaimer_impl(
        envelope,
        state,
        scope_en=scope_en,
        scope_ja=scope_ja,
        rendered=rendered,
        preserve_as=preserve_as,
    )
    secret = credential_in_text(envelope.get("output"))
    if not secret:
        return envelope

    language = decided_language(state)
    if _came_from_the_sender(state, secret):
        # Theirs to remove, so they are told -- and delivered as SUCCESS, because under
        # `status: error` the runner raises and the sentence reaches nobody.
        envelope["output"] = CREDENTIAL_IN_REQUEST_NOTICE + disclaimer(language, scope_en=scope_en, scope_ja=scope_ja)
        envelope["refusal_kind"] = "input"
        envelope["status"] = "success"
    else:
        # The agent produced it. Nothing the sender can do, so no invitation to retry, and
        # the run stays an error where an operator will see it.
        envelope["output"] = CREDENTIAL_IN_OUTPUT_NOTICE + disclaimer(language, scope_en=scope_en, scope_ja=scope_ja)
        envelope["refusal_kind"] = "egress"
        envelope["status"] = "error"
    for key in ("formatted_output", "result"):
        if key in envelope:
            envelope[key] = envelope["output"]
    return envelope
