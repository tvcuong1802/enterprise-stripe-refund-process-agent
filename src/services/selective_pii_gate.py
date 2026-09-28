"""A narrowed S-2 input gate for agents whose SUBJECT is an identifier.

Byte-identical in every template that uses it. What each agent narrows is declared at the
call site, not here, so the decision is visible in that repo's own node.

WHY THIS EXISTS
---------------
`FunctionNode._security_gate_input()` masks every `detect_pii` finding in `user_input`,
`validated_input` and `llm_response`, before `execute()` runs. For most agents that is
exactly right: the identifier is incidental to the work.

For a class of SaaS-operation agents it destroys the work itself. Measured 2026-09-03 by
running the graph, not by reading it:

    (internal reference removed) pre_process received: 'Seikyuu mail wa [MASKED] desu.'
    CMN-C1-596 pre_process received: 'Refund charge ch_3Abc for 5000 JPY, customer [MASKED]'

The email IS the instruction. Masked, (internal reference removed) answers "No email or customer id found"
on every real request -- an agent that cannot do the one thing it exists to do, while
reporting success.

WHY IT IS DONE THIS WAY
-----------------------
`FunctionNode._security_gate_input` is `@final`: overriding it raises TypeError at class
definition, by design, so the default scan cannot be silently dropped. `_PII_SCAN_FIELDS`
is a module constant in the wheel with no configuration, no environment override and no
trust-level condition -- there is no knob.

`BaseNode._security_gate_input` is `@abstractmethod`, and a node that inherits BaseNode
directly implements it itself. This is a documented, framework-sanctioned position, not a
bypass: `GraphNode` and `RemoteAgentNode` in the wheel both take it, and the framework contract
states that a BaseNode subclass "must override the @abstractmethod versions directly" and
that a deliberate choice there "is not a bypass".

WHAT IS AND IS NOT NARROWED
---------------------------
This gate still masks EVERY PII class the default gate masks, with exactly one exception:
the finding types the agent declares it operates on. Everything else -- names, phone
numbers, national identifiers, payment card numbers -- is masked exactly as before. The
injection check is NOT narrowed: it runs unchanged, on the same fields, with the same
blocking behaviour, because an instruction to the agent is not the same thing as a
customer identifier.

An agent that narrows `email` still masks a phone number in the same sentence. An agent
that narrows nothing gets the default behaviour and should use FunctionNode instead.

WHAT THE CALLER OWES
--------------------
An identifier that survives this gate is ordinary personal data under the agent's care:

* It reaches the provider API, which is the point.
* It must NOT be written into an audit payload -- `emit_trace_event` payloads are
  structured, already-safe fields only.
* It must NOT be echoed into the reply beyond what the reader supplied themselves.
* It IS checkpointed with state, like any other request field. That is processing the
  data the caller sent for this purpose, not storing a credential; a credential in state
 remains forbidden (the framework contract) and this changes nothing about that.
"""

from __future__ import annotations

from typing import Any

from framework.schemas.agent_status import AgentStatus
from framework.security.credential_detector import detect_credentials_in_value
from framework.security.injection_policy import evaluate_injection_content
from framework.security.pii_detector import detect_pii
from framework.security.pii_masking import mask_pii

#: The same fields the default gate scans. Kept in step with the wheel deliberately: this
#: gate narrows WHICH FINDINGS are masked, never WHICH FIELDS are looked at.
PII_SCAN_FIELDS = ("user_input", "validated_input", "llm_response")

#: The default gate blocks injections on these. Unchanged here, on purpose.
INJECTION_BLOCKING_FIELDS = ("user_input", "validated_input")


def selective_security_gate_input(
    state: Any,
    *,
    operates_on: tuple[str, ...],
    node_name: str,
) -> Any:
    """The default S-2 gate, minus the finding types this agent operates on.

    `operates_on` is a closed set of `detect_pii` finding types -- "email", "phone",
    "name", "ssn", "my_number", "credit_card". Anything not named here is masked exactly
    as the default gate masks it.
    """
    state = dict(state)
    for field in PII_SCAN_FIELDS:
        raw = state.get(field, "")
        if not isinstance(raw, str) or not raw:
            continue
        findings = detect_pii(raw)
        # The whole narrowing, in one line: drop the findings this agent is FOR, mask the
        # rest. Filtering findings rather than skipping the field keeps every other PII
        # class covered in the same sentence.
        findings = [f for f in findings if f.get("type") not in operates_on]
        if findings:
            state[field] = mask_pii(raw, findings)

    for field in INJECTION_BLOCKING_FIELDS:
        raw = state.get(field, "")
        if not isinstance(raw, str) or not raw:
            continue
        state = evaluate_injection_content(raw, field=field, state=state, node_name=node_name)
        if state.get("status") == AgentStatus.ERROR.value:
            return state
    return state


def default_security_gate_output(result: dict[str, Any], *, node_name: str) -> dict[str, Any]:
    """The DEFAULT S-3 gate, unchanged, for a node that inherits BaseNode directly.

    Narrowing S-2 costs the node FunctionNode's S-3 as well, because both come from the
    same base. Nothing about the egress scan is being reconsidered here -- a credential
    in the output is blocked exactly as before -- so this reproduces the wheel's own
    implementation rather than leaving a node with no S-3 at all, which is what "inherit
    BaseNode" would otherwise mean.
    """
    for key, value in result.items():
        findings = detect_credentials_in_value(value)
        if findings:
            raise RuntimeError(
                f"S-3 output gate: credential pattern '{findings[0]['type']}' "
                f"detected in result['{key}']. "
                "Output blocked. Remove credentials from agent output."
            )
    return result
