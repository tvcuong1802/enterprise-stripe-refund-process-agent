"""Who is allowed to trigger a write, and what the reader is told when they are not.

Byte-identical fleet-wide (`shared-file-drift` watches it). The rule it enforces is a
CoE ruling, not a local preference: **write modes are out of scope for the Marketplace
entry point**, and lowering a template's own write authorization to
``VERIFIED_EXTERNAL`` so that they become reachable there is explicitly prohibited
(an internal ruling, recorded in the framework contract; (internal reference removed) lists that lowering as its first
option and it is rejected). ``STG_INTERNAL_RUNNER_TOKEN`` is scoped to the ``deploy-stg``
smoke check and does not transfer here.

Why a shared module and not three lines in each node:

1. **The check cannot live in the node.** ``BaseNode.__call__()`` runs the S-1 gate
   BEFORE ``execute()``, so a node that honestly declares ``INTERNAL`` never reaches its
   own code on a Marketplace call -- the run ends at ``status: error``, the runner raises
   on it, and the caller is told "agent failed" for a request that was merely not
   permitted. The declaration stays honest and the SKIP happens upstream, in the graph.

2. **The reader has to be told something true.** "Agent failed" and a blank screen are
   both wrong: the request was understood and refused. The notice below says which action
   was not performed and where it can be performed instead, without naming a trust level,
   a token, or any other internal architecture.

3. **The comparison is easy to get subtly wrong.** ``state["caller_trust_level"]`` arrives
   as a string on one path and as an enum on another, and a missing value must read as the
   LEAST privilege, never the most. One implementation, tested both ways.
"""

from __future__ import annotations

from typing import Any

#: Least privileged first. A level this module does not know ranks as ANONYMOUS: an
#: unrecognised value is not evidence of privilege, and defaulting upward is how a
#: typo becomes an authorization.
_RANK = {"anonymous": 0, "verified_external": 1, "internal": 2}

#: What a write path must require. Named rather than inlined so a repo cannot quietly
#: pass "verified_external" here and still look like it is calling the shared check.
WRITE_TRUST = "internal"


def caller_trust(state: Any) -> str:
    """The caller's trust level as a lowercase name, or "anonymous" when unknown.

    Accepts the string the framework puts in state and the enum some tests pass, because
    both really occur. Anything else is treated as no privilege at all.
    """
    raw = state.get("caller_trust_level") if isinstance(state, dict) else None
    if raw is None:
        return "anonymous"
    value = getattr(raw, "value", raw)
    name = str(value).strip().lower()
    # "TrustLevel.INTERNAL" -- what str() on the enum gives on some paths.
    if "." in name:
        name = name.rsplit(".", 1)[-1]
    return name if name in _RANK else "anonymous"


def caller_may_write(state: Any, minimum: str = WRITE_TRUST) -> bool:
    """True only when the caller reaches `minimum`. Unknown input means no."""
    required = _RANK.get(str(minimum).strip().lower())
    if required is None:
        return False
    return _RANK[caller_trust(state)] >= required


def write_unavailable_notice(
    language: str | None,
    *,
    action_en: str,
    action_ja: str,
    channel_en: str = "an authorised company system",
    channel_ja: str = "社内の権限あるシステム",
) -> str:
    """What the reader is told when the write was not performed.

    The wording deliberately avoids the word "internal". It is ordinary English for
    "inside the company", but it is also the name of the trust level being enforced, and
    a refusal must not hand the caller the name of the gate to aim at next. The test
    forbids the bare word for that reason; the phrasing below loses nothing by avoiding it.

    `action_*` names the specific thing that did not happen -- "the admin role was not
    revoked", not "the operation failed". A reader who is told the generic sentence
    cannot tell a refusal from a bug, and will retry.

    Bilingual when `language` is empty or unrecognised: that is the branch where the
    reader's language is least knowable, and a notice they cannot read is no notice.
    """
    english = (
        f"**{action_en} was not performed.** This action is only available through "
        f"{channel_en}, not through this channel. Everything above it was read, not "
        "changed."
    )
    japanese = (
        f"**{action_ja}は実行していません。** この操作は{channel_ja}からのみ実行できます。"
        "本チャネルでは実行されません。上記は参照のみで、変更は行っていません。"
    )
    normalised = (language or "").strip().lower()[:2]
    if normalised == "en":
        return english
    if normalised == "ja":
        return japanese
    return f"{english}\n\n{japanese}"
