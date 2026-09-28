"""PB — the model is on the live invoke path, and its answer reaches the reader.

Buoc 0d asks three things of a template: that a client EXISTS, that it RUNS, and that it
sits where it adds something. Static analysis answers only the first. This file answers
the second, and it is the artefact `llm-live-path-untested` demands.

Copy this file verbatim into `tests/unit/test_llm_live_path.py` and fill the two markers
at the bottom. Keep everything above them byte-identical fleet-wide: the whole point is
that one patch fixes every repo, verified with sha256, the same mechanism as kb_port.py.

Two things this pins that a naive version does not:

1. **The call count alone is vacuously green.** A client can be built, called, and then
   ignored by the branch that produces the answer -- which reads as success and costs
   money for nothing. So the degraded output is compared too: if answering with a model
   and answering without one produce the same text, the model is decoration.

2. **BaseGraph.invoke() does NOT bind the secrets ContextVar.** The Marketplace runner
   does, at marketplace_app.py:144 (`with bound_secrets(...)`). A test that skips that
   binding makes every repo whose client comes from the shared llm_provider.py look like
   it never calls the model, because InvocationContext.from_state() then sees a
   NullProvider, require() raises, and build_llm_client() swallows it and returns None.
   Measured 2026-08-27: four repos looked broken and none of them were.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[2]

# Azure needs all three: a key alone cannot say which resource or which deployment to
# route to. The endpoint must be the BARE resource URL -- the suffixed form in the
# platform README makes the real client raise at construction.
_SECRETS = {
    "AZURE_OPENAI_API_KEY": "mock-key-for-testing",
    "AZURE_OPENAI_ENDPOINT": "https://example-resource.services.ai.azure.com",
    "AZURE_OPENAI_DEPLOYMENT": "gpt-5.4-mini",
}


class _CountingLLM:
    """Stands in for AzureOpenAIClient and records every prompt handed to it."""

    calls: list[str] = []

    def __init__(self, config: Any = None, **kwargs: Any) -> None:
        self._config = config

    def _record(self, payload: Any) -> str:
        type(self).calls.append(repr(payload)[:200])
        # MODEL_REPLY may be a callable taking the prompt. A pipeline with TWO model
        # consumers can validate two different shapes -- measured on (internal reference removed), where
        # extraction json-parses the whole reply and synthesis then requires five
        # "**Why N:**" markers, and synthesis is only reached when extraction produced
        # events. No single static string passes both, so the fill-in dispatches.
        if callable(MODEL_REPLY):
            return str(MODEL_REPLY(str(payload)))
        return MODEL_REPLY

    def complete(self, messages: Any, **kwargs: Any) -> dict[str, str]:
        return {"content": self._record(messages)}

    def generate(self, prompt: Any, **kwargs: Any) -> str:
        return self._record(prompt)

    def invoke(self, prompt: Any, **kwargs: Any) -> str:
        return self._record(prompt)


def _provider(values: dict[str, str]) -> Any:
    from framework.secrets.base import SecretProvider

    class _P(SecretProvider):  # type: ignore[misc]
        def get(self, key: str, default: Any = None) -> Any:
            return values.get(key, default)

        def require(self, key: str) -> Any:
            value = values.get(key)
            if not value:
                raise KeyError(key)
            return value

    return _P()


def _graph_class() -> Any:
    """Read the entrypoint from the manifest rather than hardcoding ``Graph``.

    `class:` in config/agent.yaml is the source of truth. Repos exist whose graph class
    has another name, or that ship more than one graph module; hardcoding picks the
    wrong one silently.
    """
    manifest = (_REPO / "config" / "agent.yaml").read_text(encoding="utf-8")
    match = re.search(r'^class:\s*"?([^"#\n]+)"?', manifest, re.M)
    assert match, "config/agent.yaml declares no class:"
    module, name = match.group(1).strip().rsplit(".", 1)
    return getattr(importlib.import_module(module), name)


def _invoke(monkeypatch: pytest.MonkeyPatch, text: str, *, with_secrets: bool) -> str:
    from framework.schemas.invocation_context import InvocationContext, TrustLevel
    from framework.secrets.context import bound_secrets

    azure = pytest.importorskip(
        "shared.services.llm.azure_openai_client",
        reason="registry wheel 1.0.1 does not ship the Azure client module",
    )
    monkeypatch.setattr(azure, "AzureOpenAIClient", _CountingLLM)
    _CountingLLM.calls = []

    agent = _graph_class()()
    agent.provision_secrets(_provider(dict(_SECRETS) if with_secrets else {}))
    agent.compile()
    ctx = InvocationContext(
        session_id="llm-live-path",
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        caller_id="",
    )
    # Bind exactly as the runner does. Without this the shared llm_provider.py path
    # cannot see the provisioned secrets and degrades -- see the module docstring.
    with bound_secrets(agent._secrets_provider):
        result = agent.invoke(text, ctx=ctx)
    output = result.get("output") if isinstance(result, dict) else result
    return str(output)


def test_the_model_is_called_on_the_normal_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """At least one model call happens for an input this agent is actually for."""
    _invoke(monkeypatch, LIVE_INPUT, with_secrets=True)
    assert _CountingLLM.calls, (
        "no model call on the normal invoke path. Either the client is never built "
        "(check that it is created in execute() from ctx.secrets, not in __init__), or "
        "LIVE_INPUT does not reach the branch that needs a model."
    )


def test_answering_without_a_model_produces_different_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model must change the answer, not merely be invoked.

    This is the assertion that stops the previous test from passing vacuously. A client
    that is built, called and then discarded satisfies a call count and nothing else.
    """
    with_model = _invoke(monkeypatch, LIVE_INPUT, with_secrets=True)
    without_model = _invoke(monkeypatch, LIVE_INPUT, with_secrets=False)
    assert with_model.strip() != without_model.strip(), (
        "identical output with and without a model: the call is decoration. Either the "
        "result is assembled before the model is consulted, or the model's reply is "
        "discarded downstream."
    )


def test_the_degraded_path_does_not_fabricate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a model the agent must not answer as if it had one.

    The expensive failure in this fleet is not a crash, it is a confident wrong answer:
    a stand-in that returns a complete-looking report for every input, with
    status: success, and no way for the reader to tell it apart from real work.
    """
    without_model = _invoke(monkeypatch, LIVE_INPUT, with_secrets=False)
    assert DEGRADED_SIGNAL in without_model, (
        "the no-model path gives no signal that the answer is degraded. It must say so "
        "in words the reader understands -- without naming secrets, providers or any "
        "other internal architecture."
    )


# -----------------------------------------------------------------------------
# Fill these three. Everything above stays byte-identical across repos.
# -----------------------------------------------------------------------------

# An input this agent is genuinely FOR. Reuse deploy/invoke_payload.json when it carries
# one -- then the test and the Stage-5 evidence exercise the same path.
# A generic sentence is the wrong choice: agents that short-circuit on unusable input
# will correctly answer with guidance and never reach the model, and the test then
# reports a defect that is not there.
LIVE_INPUT = "ch_3Abc no 5000 en wo henkin shite kudasai. Henpin taiou desu."

# A phrase the degraded answer always contains. Take it from the constant in src/ so the
# test breaks when the wording changes, rather than silently passing on a stale string.
# What the stand-in model answers. It must be SHAPED FOR THIS AGENT: a node that parses
# JSON, or that validates citations before accepting an answer, will correctly discard a
# generic sentence -- and the test then reports a defect that is not there. This is the
# marker that cost two false alarms while probing the fleet on 2026-08-27.
# Shaped for the intake contract: it reads `language`, not `answer_language`.
# A reply with the wrong key parses fine and is then ignored -- the call happens,
# the decision does not, and only comparing the two outputs catches it.
MODEL_REPLY = '{"language": "ja", "fields": {}, "verdict": "in_scope", "suggestion": ""}'

# A phrase the degraded answer always contains. Take it from the constant in src/ so the
# test breaks when the wording changes, rather than silently passing on a stale string.
DEGRADED_SIGNAL = "AI-generated draft"
