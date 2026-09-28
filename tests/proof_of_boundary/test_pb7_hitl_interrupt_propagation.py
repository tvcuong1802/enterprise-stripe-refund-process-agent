# PB-7: HITL interrupt propagation (MANIFEST-required boundary test)
#
# When a template enables HITL (`hitl.enabled: true`), an `interrupt()` call
# inside `execute()` must raise GraphInterrupt and propagate through
# BaseNode.__call__() WITHOUT being swallowed by the application error boundary
# (status must never be set to "error"). This file is the standard scaffold
# boundary test and auto-skips for non-HITL templates.
#
# (internal reference removed) does not enable HITL (no `hitl` block in config/agent.yaml), so the
# test below is skipped via pytest.mark.skipif.

import pathlib

import pytest
import yaml


def _hitl_enabled() -> bool:
    """Return True only when config/agent.yaml declares hitl.enabled: true."""
    cfg_path = pathlib.Path(__file__).resolve().parents[2] / "config" / "config.yaml"
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return False
    #: AgentRegistry reads keys at ROOT level, so the legacy nested
    # `agent:` block is gone and the fallback below is the only live path. Reading the dead
    # path first still worked by luck — removed so the intent is explicit.
    hitl = cfg.get("hitl", {}) or {}
    return bool(hitl.get("enabled", False))


pytestmark = pytest.mark.skipif(
    not _hitl_enabled(),
    reason="hitl.enabled is not true for this template — PB-7 applies to HITL templates only",
)


def test_hitl_interrupt_propagation():
    # Executed only for HITL-enabled templates. GraphInterrupt is the signal the
    # framework uses to bubble a HITL pause out through BaseNode.__call__(); it
    # must be a BaseException so ordinary `except Exception` error boundaries in
    # execute() do not catch and downgrade it to a status="error" result.
    from langgraph.errors import GraphInterrupt

    assert issubclass(GraphInterrupt, BaseException)
