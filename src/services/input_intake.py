"""Read a free-form message once: its language, the fields the agent needs, and whether
the request is something this agent can do.

One model call, not three. Language detection, field extraction and a scope opinion all
read the same sentence, and every template invokes on every request -- splitting them
triples latency and cost for nothing.

The division of labour is fixed and deliberate:

    the model READS          the agent DECIDES
    ---------------          -----------------
    what language this is    which of the declared languages to answer in
    which fields appear      whether those values are acceptable
    whether it fits          whether to refuse

The model widens what a user may TYPE. It never widens what the agent will DO. Every
value it returns is checked for membership in a set the template declares; anything else
is dropped, and the deterministic path decides instead. Three consequences worth naming:

* A suggestion is returned as an INDEX into the template's own capability list, never as
  free text, so the agent cannot offer work it does not do -- and an invented capability
  reads exactly as convincingly as a real one.
* ``out_of_scope`` needs TWO independent signals: the model says no AND the agent's own
  deterministic check says no. Measured on (internal reference removed) (2026-08-27), the model returned
  "no connector matches" for a valid Japanese request and again for a Vietnamese one; a
  model-only refusal would have turned both into a dead end with no way to appeal.
* When the model is unavailable the result still comes back, marked ``source="fallback"``,
  so a degraded run is visible in the trace instead of looking like a confident answer.

Call this from inside ``execute()``. Calling it earlier -- in a graph hook or before the
node's security gate -- sends raw input, PII included, to the model provider.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

DEFAULT_INTAKE_POLICY: dict[str, Any] = {
    # The languages this agent can actually produce. Anything else the reader types is
    # answered in `default_language` -- an agent whose renderer has no labels for a
    # language must not be steered into it by the message it was given.
    "languages": ("en", "ja"),
    "default_language": "en",
    # field name -> allowed values, or None for a free string the agent validates itself.
    "fields": {},
    # What this agent can do, in the reader's words. A suggestion is one of these, chosen
    # by index. Keep them short enough to print back as guidance.
    "capabilities": (),
    # Few-shot pairs for THIS agent: [{"message": "...", "expect": {...}}, ...].
    # Declared per template -- see _render_examples for why they are not shared.
    "examples": (),
    # How much of the message to show the model.
    "max_chars": 2000,
}

_PROMPT = """You are routing one message sent to an assistant. Do not answer the message.

How to read it, in this order:

1. LANGUAGE. Decide which language the writer would want to READ, which is not always the
   script they typed. A question written in romanised Japanese wants Japanese. A question
   in a language the assistant does not have is answered in the default, so if the message
   is in none of {languages}, say "other" -- do not pick the nearest one.
2. FIELDS. Fill a field only from what the message actually says. Do not infer a value
   because it is the most common one, and do not translate a value into a listed one that
   the writer did not mean. Leave a field out when the message does not carry it: a
   missing field is recoverable downstream, a wrong one is not.
3. FIT. Ask whether this assistant, as described below, could serve the request as
   written. Say "unsure" rather than "no" when the request is adjacent to what it does --
   "no" is reserved for a request plainly about something else.

Reply with JSON only -- no prose, no code fence -- with exactly these keys:
  "language": one of {languages}, or "other"
  "fields": an object holding any of these keys you can fill: {fields}
  "fits": "yes", "no", or "unsure"
  "suggestion": a number from the capability list, or null

The assistant can:
{capabilities}
{examples}
Message:
{message}"""

_EXAMPLE_BLOCK = """
Worked examples for this assistant:
{examples}"""


def _render_examples(examples: Any) -> str:
    """Few-shot pairs, declared per agent. Malformed entries are skipped, not fatal.

    These are the part of the prompt that carries what a general instruction cannot: what
    "adjacent but not ours" looks like for THIS agent, and which values in a message are
    the fields rather than decoration. They belong in the template's own policy for the
    same reason the scope wording does -- a shared example set would teach every agent the
    first agent's domain.
    """
    if not isinstance(examples, (list, tuple)):
        return ""
    rendered = []
    for entry in examples:
        if not isinstance(entry, dict):
            continue
        message, expected = entry.get("message"), entry.get("expect")
        if not isinstance(message, str) or not isinstance(expected, dict):
            continue
        rendered.append(f"Message: {message}\nReply: {json.dumps(expected, ensure_ascii=False)}")
    if not rendered:
        return ""
    return _EXAMPLE_BLOCK.format(examples="\n\n".join(rendered)) + "\n"


def understand_input(
    message: str,
    llm: object | None = None,
    *,
    policy: dict[str, Any] | None = None,
    declared_language: str | None = None,
    deterministic_in_scope: bool | None = None,
    script_language: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Return a flat, JSON-serialisable result. Never raises on model behaviour.

    ``deterministic_in_scope`` is the agent's own verdict, computed by whatever mechanism
    it already trusts. It is the half of the two-signal rule the model cannot influence:
    pass ``True`` and no model reply can produce ``out_of_scope``.
    """
    p: dict[str, Any] = {**DEFAULT_INTAKE_POLICY, **(policy or {})}
    languages = tuple(str(c).lower() for c in p["languages"]) or ("en",)
    default = str(p["default_language"]).lower()
    if default not in languages:
        default = languages[0]
    fields_spec: dict[str, Any] = dict(p["fields"]) if isinstance(p["fields"], dict) else {}
    capabilities = tuple(str(c) for c in (p["capabilities"] or ()))

    result: dict[str, Any] = {
        "answer_language": default,
        # WHERE the language came from, so a caller can tell a decision from a default.
        # This function always returns a member of the declared set -- that is its
        # contract -- so "en" alone cannot say whether anyone actually read English or
        # whether nothing was legible and the default filled the hole. A caller that
        # cannot tell those apart prints a one-language liability notice at exactly the
        # moment the reader's language is least known (measured on (internal reference removed),
        # 2026-09-11: "?!?! ... 1234 ###" got a Japanese-only trailer).
        "language_source": "default",
        "fields": {},
        "missing": sorted(fields_spec),
        "verdict": "in_scope" if deterministic_in_scope is not False else "unclear",
        "suggestion": "",
        "source": "fallback",
    }

    raw = _ask_json(
        llm,
        message,
        languages,
        fields_spec,
        capabilities,
        _render_examples(p.get("examples")),
        int(p["max_chars"]),
    )

    # Language: a declaration outranks the model, the model outranks the script, and the
    # script outranks the default. Each step is skipped when it names something outside
    # the declared set -- an operator typo and a hallucinated code fail the same way.
    chosen = _member(declared_language, languages)
    source = "declared" if chosen is not None else "default"
    if chosen is None and raw is not None:
        chosen = _member(raw.get("language"), languages)
        if chosen is not None:
            source = "model"
    if chosen is None and script_language is not None:
        try:
            chosen = _member(script_language(message or ""), languages)
        except Exception:
            chosen = None
        if chosen is not None:
            source = "script"
    result["answer_language"] = chosen or default
    result["language_source"] = source

    if raw is None:
        return result
    result["source"] = "model"

    got = raw.get("fields")
    if isinstance(got, dict):
        clean: dict[str, Any] = {}
        for name, allowed in fields_spec.items():
            value = got.get(name)
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            if allowed is None:
                clean[name] = value if isinstance(value, (str, int, float, bool)) else str(value)
            else:
                match = _member(value, tuple(str(a) for a in allowed), lowercase=False)
                if match is not None:
                    clean[name] = match
        result["fields"] = clean
        result["missing"] = sorted(n for n in fields_spec if n not in clean)

    fits = str(raw.get("fits", "")).strip().lower()
    if fits == "no" and deterministic_in_scope is False:
        result["verdict"] = "out_of_scope"
    elif fits in ("no", "unsure") and deterministic_in_scope is not True:
        result["verdict"] = "unclear"

    index = raw.get("suggestion")
    if isinstance(index, bool):
        index = None
    if isinstance(index, (int, float)) and capabilities:
        position = int(index)
        if 1 <= position <= len(capabilities):
            result["suggestion"] = capabilities[position - 1]
    return result


def _member(value: Any, allowed: tuple[str, ...], *, lowercase: bool = True) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if lowercase:
        text = re.sub(r"[^a-z]", "", text.lower())[:2]
        return text if text in allowed else None
    for candidate in allowed:
        if text.lower() == candidate.lower():
            return candidate
    return None


def _ask_json(
    llm: object | None,
    message: str,
    languages: tuple[str, ...],
    fields_spec: dict[str, Any],
    capabilities: tuple[str, ...],
    examples_block: str,
    max_chars: int,
) -> dict[str, Any] | None:
    """One call, parsed defensively. Returns None whenever the reply is not usable."""
    if llm is None:
        return None
    listing = "\n".join(f"  {i}. {c}" for i, c in enumerate(capabilities, 1)) or "  (not declared)"
    prompt = _PROMPT.format(
        languages=", ".join(f'"{c}"' for c in languages),
        fields=", ".join(f'"{name}"' for name in fields_spec) or "(none)",
        capabilities=listing,
        examples=examples_block,
        message=(message or "")[:max_chars],
    )
    try:
        reply = _ask(llm, prompt)
    except Exception:
        return None
    text = str(reply).strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _ask(llm: object, prompt: str) -> str:
    """Ask one question and return text, across the client shapes in the fleet.

    Three things go wrong here and each one costs a silent fallback rather than an error,
    so all three are handled explicitly:

    * **Which method.** ``complete(messages)`` is the canonical ``BaseLLM`` contract and
      is tried first. ``generate`` is tried only after it, because a client can expose
      BOTH -- ``AzureOpenAIClient`` has a ``generate`` that takes a message list and
      reaches for ``msg.content``, so handing it a string raises
      ``AttributeError: 'str' object has no attribute 'content'``. Preferring the method
      that merely EXISTS picked the wrong one on the real client (measured on
      (internal reference removed) against Azure, 2026-08-28).
    * **What comes back.** ``complete()`` answers ``{"content": str, ...}``, so
      ``str(response)`` is a Python dict repr -- single quotes, parses as nothing.
    * **Argument shape.** A client may want a string where another wants a list; a
      ``TypeError`` or ``AttributeError`` from one shape falls through to the next
      rather than ending the call.
    """
    messages = [{"role": "user", "content": prompt}]
    attempts = (
        ("complete", messages),
        ("generate", messages),
        ("generate", prompt),
        ("complete", prompt),
    )
    last: Exception | None = None
    for name, argument in attempts:
        method = getattr(llm, name, None)
        if not callable(method):
            continue
        try:
            reply = method(argument)
        except (TypeError, AttributeError) as exc:
            last = exc
            continue
        if isinstance(reply, dict):
            for key in ("content", "text", "output"):
                value = reply.get(key)
                if isinstance(value, str):
                    return value
        return str(reply)
    raise last or AttributeError("llm exposes neither complete() nor generate()")
