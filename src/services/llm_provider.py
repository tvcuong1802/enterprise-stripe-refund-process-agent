"""Marketplace LLM wiring — build the client per invocation, from ctx.secrets.

Two problems this solves, neither visible until the agent runs on the platform.

1. The runner constructs the graph as ``Graph()`` with no arguments
   (``marketplace_app.py:95``), so an injected ``llm_client`` is always ``None``
   there and every LLM-backed node takes its degradation branch. The run still
   reports ``status: success`` while producing placeholder content — which then
   passes whatever checks look at that content. The agent therefore has to build
   its own client.

2. Nodes call ``llm.generate(prompt)`` with a plain string. ``BaseLLM`` exposes
   ``complete(messages)`` taking a message list; ``.generate`` only resolves
   through ``AzureOpenAIClient.__getattr__`` down to LangChain's
   ``BaseChatModel.generate``, which expects ``list[list[BaseMessage]]`` and fails
   on a string. ``_GenerateAdapter`` bridges the two shapes so call sites keep the
   name they already use.

Build inside ``execute()``, never in ``__init__``: secrets are per-invocation, and
``compile()`` runs before the provider exists. Do not assign the client to ``self``
either — a node instance is shared across invocations, and some graphs fan out with
a ThreadPoolExecutor.
"""

from __future__ import annotations

from typing import Any

from framework.schemas.invocation_context import InvocationContext

# Settings forwarded from config.yaml's `llm:` block. Without them the model runs at
# the provider default temperature with no response ceiling, so the same input yields
# a different answer run to run — the values are declared in config precisely to stop
# that, and forwarding them is what makes the declaration real.
_LLM_PASSTHROUGH = ("max_tokens", "temperature", "top_p", "timeout", "seed")


class _GenerateAdapter:
    """Expose ``.generate(str) -> str`` on top of ``BaseLLM.complete(list)``."""

    def __init__(self, llm: Any) -> None:
        self._llm = llm

    def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        """Pass ``BaseLLM.complete(messages)`` straight through.

        The adapter exists to give nodes a ``.generate(str)`` surface, but the bridge
        has to work in BOTH directions: a node that already speaks the canonical
        ``complete(messages) -> {"content": ...}`` contract would otherwise get an
        AttributeError from the adapter, and such nodes typically wrap the call in a
        broad ``except`` and fall back — so the model silently never ran while the run
        reported success. Measured on (internal reference removed) (2026-08-27): zero model calls, no
        error anywhere.
        """
        result = self._llm.complete(messages, **kwargs)
        return result if isinstance(result, dict) else {"content": str(result)}

    def generate(self, prompt: str) -> str:
        response = self._llm.complete([{"role": "user", "content": prompt}])
        if isinstance(response, dict):
            # isinstance() narrows `response` to dict, but .get() on it still yields Any,
            # so the one-line form returned Any from a function annotated -> str. That is
            # not only a typing complaint: a provider answering {"content": None} would
            # hand the caller a None where every consumer expects text. Narrow the VALUE
            # too, and coerce what is left.
            content = response.get("content", "")
            return content if isinstance(content, str) else str(content)
        return str(response)

    def invoke(self, prompt: str) -> str:
        """Third surface found in the fleet: ``.invoke(prompt) -> str``.

        (internal reference removed)'s extraction node calls ``llm_client.invoke(prompt)``. Without this
        the adapter raises AttributeError inside a broad ``except`` and the node takes
        its fail-closed branch -- an empty summary, reported as success. The bridge has
        to cover every surface a node in this fleet actually uses, or wiring a client
        looks identical to not wiring one.
        """
        return self.generate(prompt)


def build_llm_client(state: dict[str, Any], llm_config: dict[str, Any] | None = None) -> Any | None:
    """Build an Azure client from invocation-scoped secrets.

    Returns ``None`` when the secrets or the optional dependency are absent, so the
    caller keeps its existing degradation path rather than failing the whole run.
    The caller is responsible for making that degradation visible — a placeholder
    result that reads like a real one is the failure this wiring exists to avoid.
    """
    try:
        ctx = InvocationContext.from_state(state)
        from shared.services.llm.azure_openai_client import AzureOpenAIClient

        settings: dict[str, Any] = {
            "api_key": ctx.secrets.require("AZURE_OPENAI_API_KEY"),
            "azure_endpoint": ctx.secrets.require("AZURE_OPENAI_ENDPOINT"),
            "azure_deployment": ctx.secrets.require("AZURE_OPENAI_DEPLOYMENT"),
        }
        for key in _LLM_PASSTHROUGH:
            value = (llm_config or {}).get(key)
            if value is not None:
                settings[key] = value
        client = AzureOpenAIClient(settings)
    except Exception:  # noqa: BLE001 — missing secrets/extra → documented degradation
        return None
    return _GenerateAdapter(client)


def build_llm_client_from_provider(provider: Any, llm_config: dict[str, Any] | None = None) -> Any | None:
    """Same client, built from a SecretProvider directly instead of from State.

    `build_llm_client()` reads the provider off `InvocationContext.from_state(state)`,
    which needs a real invocation state -- `from_state({})` raises KeyError on
    `correlation_id`, and the caller's `except` then swallows it and returns None. So
    every entry point that has a provider but no state -- the standalone HTTP adapter,
    a service helper called before the graph runs -- silently got no client at all while
    reporting healthy. Measured on (internal reference removed) and (internal reference removed), 2026-09-04.

    Returns None when a secret or the optional dependency is absent, exactly as the
    state-based version does, so callers keep whichever degradation path they chose.
    """
    try:
        from shared.services.llm.azure_openai_client import AzureOpenAIClient

        settings: dict[str, Any] = {
            "api_key": provider.require("AZURE_OPENAI_API_KEY"),
            "azure_endpoint": provider.require("AZURE_OPENAI_ENDPOINT"),
            "azure_deployment": provider.require("AZURE_OPENAI_DEPLOYMENT"),
        }
        for key in _LLM_PASSTHROUGH:
            value = (llm_config or {}).get(key)
            if value is not None:
                settings[key] = value
        client = AzureOpenAIClient(settings)
    except Exception:  # noqa: BLE001 — missing secrets/extra → documented degradation
        return None
    return _GenerateAdapter(client)
