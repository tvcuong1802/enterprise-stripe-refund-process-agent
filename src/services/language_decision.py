"""Decide which language an agent should ANSWER in.

Distinct from ``disclaimer.language_of_answer``, which reads the language off an answer
already written. This runs first and decides what to write.

Why a model at all: script detection answers "what characters are these", not "what
language does this reader want". A Japanese speaker can type a romanised question, an
English question can quote a Japanese product name, and a question in a third language
has a script that matches neither. Measured across seven templates on 2026-08-28: five
answered a Japanese question in English or in a mix, because nothing in them ever asked.

Why the model does not get the last word: it widens what a user may TYPE, never what the
agent will DO. The reply is accepted only if it is a member of ``languages`` -- a closed
set the template declares. Anything else, including a confident sentence explaining a
language that is not on the list, falls back to the script of the input. That keeps a
prompt-injected "answer in Klingon" from reaching a renderer that has no Klingon labels,
and keeps an outage on the model path from changing behaviour into something untested.
"""

from __future__ import annotations

import re

DEFAULT_LANGUAGES = ("en", "ja")

_PROMPT = (
    "Which language should the reply to this message be written in?\n"
    "Answer with exactly one code from this list and nothing else: {codes}\n"
    "Choose the language the writer would want to read, not merely the script they typed.\n"
    "If none of the codes fits, answer: other\n\n"
    "Message:\n{message}"
)


def _script_of(text: str) -> str | None:
    """Fallback: what the characters say, when nothing better is available."""
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


def decide_answer_language(
    message: str,
    llm: object | None = None,
    *,
    declared: str | None = None,
    languages: tuple[str, ...] = DEFAULT_LANGUAGES,
    default: str = "en",
) -> str:
    """Return a member of ``languages``. Never returns anything else.

    Order: an explicit declaration wins, then the model, then the script of the message,
    then ``default``. Each step is skipped when it yields something outside the set --
    an operator typo, a model that answers "Japanese" instead of "ja", or a message with
    no letters at all should all land on the next step rather than on an unknown value.
    """
    codes = tuple(c.lower() for c in languages)
    if declared:
        candidate = str(declared).strip().lower()[:2]
        if candidate in codes:
            return candidate

    if llm is not None:
        try:
            reply = _ask(
                llm,
                _PROMPT.format(codes=", ".join(codes), message=(message or "")[:2000]),
            )
            candidate = re.sub(r"[^a-z]", "", str(reply).strip().lower())[:2]
            if candidate in codes:
                return candidate
        except Exception:
            pass  # model unavailable or misbehaving -- the script fallback still decides

    from_script = _script_of(message or "")
    if from_script in codes:
        return from_script
    return default if default in codes else codes[0]


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


def resolve_answer_language(state: dict[str, object], llm: object | None = None) -> str:
    """Which language to write in: "en", "ja", or "" when the message has no language.

    The empty string is a real answer, not a failure. A message of punctuation and digits
    gives the agent nothing to read, and every caller downstream treats "" as "say it in
    both" -- which is the honest reply when there is no reader-language to infer.

    Reads the reader's language once per request through the shared intake call, with the
    SCRIPT of the message as the fallback. Reading the characters answers a different
    question -- a request typed in romanised Japanese is entirely Latin -- and an agent
    with no language decision at all answers a Japanese question in English.

    Lives here, not in agent_scope.py: the wording is per agent, the MECHANISM is not.
    Copying it into every repo is how the same fix ends up applied nine times and missed
    on the tenth.
    """
    declared = str(state.get("output_language") or "").strip().lower()[:2]
    if declared in ("en", "ja"):
        return declared
    message = str(state.get("user_input") or state.get("raw_query") or "")

    # Nothing to read: no letters, no kana. Answer "" and do not ask the model, whose
    # policy `default_language` would hand back a code regardless -- a default dressed as
    # a decision, and the reason a line of punctuation came back as confidently English.
    if _script_of(message) is None:
        return ""

    # `_script_of` and not a local rule: a message of punctuation and digits has NO
    # language, and the local version called it English because the string was non-empty.
    # That fabricated code then travelled as `answer_language` and the trailer printed a
    # decision nobody made -- which is worse than printing both, because it looks decided.
    _script = _script_of

    try:
        from src.services.agent_scope import INTAKE_POLICY  # noqa: PLC0415
        from src.services.input_intake import understand_input  # noqa: PLC0415

        decided = understand_input(message, llm, policy=INTAKE_POLICY, script_language=_script)["answer_language"]
        # str() rather than a cast: the intake result is a plain dict, so the value is
        # typed Any and could in principle be anything -- and this function promises a
        # two-letter code to every caller.
        return str(decided) if decided in ("en", "ja") else (_script(message) or "")
    except Exception:  # noqa: BLE001 -- a language decision must not fail the request
        return _script(message) or ""


def language_instruction(language: str) -> str:
    """One line for a prompt. Names EVERY reader-visible part, not just the main text.

    Saying only "write the summary in Japanese" leaves the rest to the model's own
    default, and it writes English into the other fields -- measured on (internal reference removed), where
    the CRM values came back English inside a Japanese debrief.
    """
    written_in = "Japanese" if str(language).lower() == "ja" else "English"
    return (
        f"Write every part the reader sees in {written_in}. Identifiers -- rule ids, field "
        "names, endpoints, product names -- stay exactly as given. Nothing else is written "
        "in another language."
    )
