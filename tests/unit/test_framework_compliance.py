# CMN-C1-596 — Framework compliance tests (TC-01..08).

import inspect
from typing import NotRequired, get_args, get_origin, get_type_hints

import pytest

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext, TrustLevel
from framework.secrets.context import NullProvider, bound_secrets

from src.graph.graph import Graph
from src.nodes.charge_verify_node import ChargeVerifyNode
from src.nodes.invoice_check_node import InvoiceCheckNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.refund_calc_node import RefundCalcNode
from src.nodes.refund_execute_node import RefundExecuteNode
from src.schemas.state import State

ERROR = AgentStatus.ERROR.value
_ALL_NODES = (PreProcessNode, ChargeVerifyNode, RefundCalcNode, InvoiceCheckNode, RefundExecuteNode, PostProcessNode)


# ── TC-01: State is a flat TypedDict — primitives / JSON-string only ───────────
def test_tc01_state_flat_no_prohibited_types():
    own = set(State.__annotations__) - set(AgentState.__annotations__)
    assert own, "State declares no agent-specific fields"
    hints = get_type_hints(State, include_extras=True)
    for name in own:
        typ = hints[name]
        if get_origin(typ) is NotRequired:  # unwrap NotRequired[...]
            (typ,) = get_args(typ)
        assert typ is str, f"{name} is not a flat str type: {hints[name]}"


# ── TC-02: S-2 gate fires on unsafe input → ERROR (no raise) ───────────────────
def test_tc02_s2_rejects_unsafe_input():
    node = PreProcessNode()
    st = {
        "user_input": "",
        "input_context": {"instruction": "refund charge ch_1 ../../etc/passwd"},
        "error_log": [],
        "status": "",
    }
    gated = node._extra_security_gate_input(st)
    assert gated["status"] == ERROR
    out = node.execute(gated)
    assert out["status"] == ERROR


# ── TC-03: no credential field names / literals in State ───────────────────────
def test_tc03_no_credential_literals_in_state():
    src = inspect.getsource(State)
    assert "STRIPE_API_KEY" not in src  # key name lives only in services/manifest
    for name in State.__annotations__:
        assert not any(k in name.lower() for k in ("token", "secret", "password", "api_key", "credential"))


# ── TC-04: InvocationContext is never stored in State ──────────────────────────
def test_tc04_no_invocationcontext_in_state():
    for typ in State.__annotations__.values():
        assert "InvocationContext" not in str(typ)


# ── TC-05: every node emits at least one domain S-4 event in execute() ─────────
def test_tc05_audit_event_emitted(monkeypatch):
    captured = []
    import src.nodes.refund_execute_node as m

    monkeypatch.setattr(m, "emit_trace_event", lambda e, p, s: captured.append(e))
    RefundExecuteNode().execute({"validation_error": "blocked", "status": "", "node_history": [], "error_log": []})
    assert captured, "no domain emit_trace_event fired in execute()"
    assert not any(e in ("node_start", "node_complete", "node_error") for e in captured)


# ── TC-06 / TC-07: security gates are @final — overriding raises TypeError ─────
def test_tc06_security_gate_input_is_final():
    with pytest.raises(TypeError):

        class Bad(FunctionNode):  # noqa: B903
            def _security_gate_input(self, state):
                return state


def test_tc07_security_gate_output_is_final():
    with pytest.raises(TypeError):

        class Bad(FunctionNode):  # noqa: B903
            def _security_gate_output(self, result):
                return result


# ── TC-08: required_trust_level enforced — under-trust caller refused ──────────
def test_tc08_trust_level_enforced():
    g = Graph()
    g.compile()

    class FakeClient:
        def credential_available(self):
            return True

        def get_charge(self, cid):
            return {
                "found": True,
                "amount": 1000,
                "amount_refunded": 0,
                "currency": "jpy",
                "refunded": False,
                "status": "succeeded",
                "qualified_invoice_issued": True,
            }

        def create_refund(self, **kw):
            raise AssertionError("write must not be reached for an under-trusted caller")

    g._nodes["charge_verify"]._client = FakeClient()
    g._nodes["main"]._client = FakeClient()
    ctx = InvocationContext(session_id="t", caller_trust_level=TrustLevel.ANONYMOUS, caller_id="t")
    instr = "refund charge ch_1234abcd in full, I confirm"
    with bound_secrets(NullProvider()):
        out = g.invoke(instr, ctx=ctx, input_context={"instruction": instr})
    assert str(out["status"]).lower().endswith("error")


def test_tc08b_read_nodes_at_verified_external_and_the_writer_at_internal():
    """The refund node is the one node that must NOT be at VERIFIED_EXTERNAL.

    This test used to assert that level on every node, which pinned exactly the state CoE
    prohibits: VERIFIED_EXTERNAL is what run_agent_marketplace() grants every Marketplace
    user, so declaring it on the node that issues a Stripe refund makes issuing refunds
    reachable by all of them (an internal ruling / the framework contract; (internal reference removed)).

    The declaration stays honest and Graph.add_edges() routes around the node for a caller
    who cannot reach INTERNAL -- see the comment there for why the check cannot live in
    the node itself.
    """
    from src.nodes.refund_execute_node import RefundExecuteNode

    for cls in _ALL_NODES:
        expected = TrustLevel.INTERNAL if cls is RefundExecuteNode else TrustLevel.VERIFIED_EXTERNAL
        assert cls.required_trust_level == expected, (
            f"{cls.__name__} declares {cls.required_trust_level}; the node that issues a "
            "refund must require INTERNAL, and a read node must not"
        )
    assert RefundExecuteNode in _ALL_NODES, "the writer must be covered by this sweep"
