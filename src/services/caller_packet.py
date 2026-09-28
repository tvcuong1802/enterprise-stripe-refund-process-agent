"""Read the caller's structured packet, whichever channel it arrived on.

The Marketplace runner puts ONLY `conversation_history` into `input_context`. There is no
other channel: no file upload, no volume, no second field. A node that reads its domain
payload from `input_context` alone therefore receives NOTHING on the platform -- it
answers about an empty packet, or refuses, on every single request, while reporting
success. Measured across five repos on 2026-09-03; the same shape had already been
measured on (internal reference removed) and (internal reference removed).

So the packet is read from `input_context` first -- it is authoritative and, unlike
`user_input`, not PII-scanned -- and from the message when the context carries none.

Keyed on a CLOSED SET of packet fields, never on "the message parses as JSON": a caller
can legitimately send a JSON-shaped question, and treating that as a packet would route a
question into the domain pipeline. If none of the declared fields is present, this returns
the context unchanged and the node's own empty-input path runs, which is correct.
"""

from __future__ import annotations

import json
import re
from typing import Any


#: A bare `[MASKED]` sitting where a JSON value belongs -- i.e. not already inside quotes.
#: Matched after `:` or `,` or `[` so a literal "[MASKED]" inside a string is left alone.
_MASK_SENTINEL_RE = re.compile(r"(?<=[:\[,])\s*\[MASKED\]")


def caller_packet(state: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    """The packet from `input_context`, else from the message, else {}.

    `fields` is the closed set of top-level keys this agent's packet is made of. Supply
    the ones the node actually reads -- that list is the contract, and keeping it here
    beside the call site is what stops a message from being mistaken for a packet.
    """
    context = state.get("input_context") if isinstance(state, dict) else None
    context = dict(context) if isinstance(context, dict) else {}
    if any(field in context for field in fields):
        return context

    message = state.get("user_input") if isinstance(state, dict) else None
    if not isinstance(message, str) or not message.strip():
        return context
    try:
        parsed = json.loads(message)
    except (ValueError, TypeError):
        # The framework's S-2 gate masks PII-shaped values in `user_input` BEFORE any node
        # runs, and it does not know it is editing JSON: a twelve-digit revenue figure looks
        # like an account number, so `"reported_revenue": 892000000000` becomes
        # `"reported_revenue": [MASKED]` and the whole document stops parsing. The caller
        # then gets "no data provided" about a request that carried all of it.
        #
        # Quote the bare sentinel and try once more. The masked fields stay masked -- that
        # is the gate doing its job, and the reply shows them as masked -- but one masked
        # number no longer costs the entire packet.
        repaired = _MASK_SENTINEL_RE.sub('"[MASKED]"', message)
        if repaired == message:
            return context
        try:
            parsed = json.loads(repaired)
        except (ValueError, TypeError):
            return context
    if not isinstance(parsed, dict) or not any(field in parsed for field in fields):
        return context

    # The message-borne packet does not overwrite anything the context already carries:
    # conversation_history and any real context field stay as they are.
    merged = dict(parsed)
    merged.update(context)
    return merged
