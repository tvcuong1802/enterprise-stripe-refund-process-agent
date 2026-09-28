# 03 · Test Specification — CMN-C1-596 Enterprise Stripe Refund Process Agent

## Test Strategy

All tests run on the real `agenticstar-agentcore==1.0.0` wheel (CI `run-tests` arm,
wheel-era: no `stub harness`). Three layers:

- **Unit** (`tests/unit/`) — services + nodes in isolation with an injected fake Stripe
  client (no HTTP, no secrets) + framework-compliance TCs.
- **Integration** (`tests/integration/test_graph.py`) — the full compiled graph invoked
  end-to-end via `Graph().compile()` + `invoke(user_input, ctx, input_context)`, fake
  client injected on the `charge_verify` and `main` nodes.
- **Proof-of-Boundary** (`tests/proof_of_boundary/`) — framework boundary verification.

Total: **69 tests** (9 framework-compliance + 24 node + 18 service + 14 integration +
4 PB), all green on the wheel; `ruff check src/ tests/` clean.

## Framework-Compliance Tests (`tests/unit/test_framework_compliance.py`, 9)

| TC-ID | Test | Expected |
|---|---|---|
| TC-01 | `test_tc01_state_flat_no_prohibited_types` | every agent-declared State field is a flat `str` (JSON-serialized compound values); no `dict`/`list`/Pydantic |
| TC-02 | `test_tc02_s2_rejects_unsafe_input` | S-2 `_extra_security_gate_input` sets status ERROR on path-traversal/control chars (no raise) |
| TC-03 | `test_tc03_no_credential_literals_in_state` | no credential-like field names; `STRIPE_API_KEY` never in State (gate-credential-scan is the CI enforcer) |
| TC-04 | `test_tc04_no_invocationcontext_in_state` | State carries no `InvocationContext` annotation |
| TC-05 | `test_tc05_audit_event_emitted` | a domain `emit_trace_event` fires inside `execute()`; never `node_start/complete/error` |
| TC-06 | `test_tc06_security_gate_input_is_final` | overriding `_security_gate_input` on a FunctionNode raises `TypeError` at class definition |
| TC-07 | `test_tc07_security_gate_output_is_final` | overriding `_security_gate_output` raises `TypeError` |
| TC-08 | `test_tc08_trust_level_enforced` + `test_tc08b_all_nodes_declare_verified_external` | an ANONYMOUS caller is refused (ERROR state, no write); all six nodes declare VERIFIED_EXTERNAL |

## Domain Unit Tests

**Services (`tests/unit/test_services.py`, 18)** — `IntentParserService` (full/partial
refund, USD→cents, JPY, NFKC full-width, EN/JA, explicit dry-run vs bare "preview" T-8,
payment_intent, missing-target raise, empty raise) and `StripeClient` (egress guard
allow/reject, charge normalization incl. `qualified_invoice_issued` absent→None). The
**verb+path assertions (N-17)** monkeypatch `requests.get`/`requests.post` and assert the
EXACT `GET https://api.stripe.com/v1/charges/{id}` and `POST https://api.stripe.com/v1/refunds`
with the `Idempotency-Key` header and the form body `{charge, amount, reason}`.

**Nodes (`tests/unit/test_nodes.py`, 24)** — per-node `execute()`: pre_process parse +
S-2 gate + JA detection; charge_verify populate/not-found/short-circuit; refund_calc
full-from-remaining, over-refund blocked, already-refunded blocked; invoice_check JPY
qualified / conservative(flag absent) / non-JPY-none; refund_execute confirmation gate
(unconfirmed→needs_confirmation no write, dry-run no write, confirmed writes with a
`cmn-c1-596-` idempotency key, deterministic key, blocked-on-validation, API-error
surfaced no crash); post_process EN/JA render + JA disclaimer + S-3 key redaction.

## Integration Tests (`tests/integration/test_graph.py`, 14)

Real compiled-graph invoke, fake client on `charge_verify` + `main`:

| Scenario | Assertion |
|---|---|
| unconfirmed full refund | "Confirmation Required", **no write** |
| confirmed full refund | "Executed", one write, amount = remaining |
| partial ¥ refund confirmed | write with the parsed amount |
| dry-run | "Dry Run", **no write** |
| over-refund | "Not Executed", **no write** |
| already-refunded | "Not Executed", **no write** |
| charge not found | "Not Executed", **no write** |
| currency mismatch (¥ on USD charge) | "does not match" refusal, **no write** |
| needs-confirmation report | surfaces the amount being confirmed |
| empty input | graceful SUCCESS, **no write** |
| Japanese instruction (confirmed) | 認証された翻訳ではありません present; one write |
| JPY qualified-invoice | 適格返還請求書 notice present |
| ANONYMOUS caller | status ERROR, **no write** (S-1) |
| refund API error | surfaced "Not Executed", no crash |

## Proof-of-Boundary Tests (`tests/proof_of_boundary/`, 4)

| PB-ID | Boundary | Test | Expected |
|---|---|---|---|
| PB-1 | BaseNode → AuditLogger | covered within PB-6 (invoke emits node_start/complete) + every node's domain `emit_trace_event` | no silent failures |
| PB-2 / PB-5 | State serialization / checkpoint safety | `test_state_file_safety` (static AST) + `TestRuntimeStateSafety.test_invoke_output_is_msgpack_safe_and_no_credential` (real invoke → JSON-serializable, no `sk_`/key leak) | primitives only, no credential |
| PB-3 | L1 → external service | `StripeClient` verb+path + egress-guard tests exercise the real REST contract via `requests` (mocked transport) | correct verb/path/host |
| PB-4 | Import isolation | `test_no_prohibited_imports_in_src` (AST) | 0 Level-0 imports |
| PB-6 | Invoke execution order | `test_call_order_for_every_node` — discovers all `src/nodes/` FunctionNodes and asserts S-1 → node_start → S-2 → execute → S-3 → node_complete | order verified for all 6 nodes |
| PB-7 | HITL interrupt | N/A — `hitl.enabled` is not set (confirmation is an explicit instruction token, not a backbone `interrupt()`) | not applicable |

| Caller without write scope, no refund_result | `caller_trust_level: verified_external` | Notice names the action that did not happen | `test_nodes.py::…::test_a_caller_who_cannot_write_gets_the_refusal_notice` |
| Caller WITH write scope, no refund_result | `caller_trust_level: internal` | No refusal notice — an empty result here means something else happened, and the notice would be false | `test_nodes.py::…::test_a_caller_who_CAN_write_is_never_told_they_cannot` |
| Completed refund (control) | result present, caller without write scope | No refusal notice; the result is reported | `test_nodes.py::…::test_a_completed_refund_is_not_reported_as_refused` |

## Test Execution Summary

- Environment: real `agenticstar-agentcore==1.0.3` wheel (read from pip in the measuring
  environment, not copied from a pin), `mypy==1.10.0` — this repo's own CI pin, which is
  the version whose verdict CI reports — Python 3.11. Measured 2026-09-15.
- Total: **146 passed / 0 failed / 1 skipped** (a vendor-only import guard, under the
  registry wheel).
- `ruff check src/ tests/` clean · `ruff format --check src/` clean · `mypy src/` clean.
- The write-gate condition was mutation-tested: 3 mutants, 3 killed. Before this round the
  second half was unpinned — dropping `caller_may_write` left the suite green while every
  empty result read as a refusal.
- Lint: `ruff check src/ tests/` clean.

## Refused input — what the sender receives (shared contract, 2026-09-15)

Measured across the fleet with a real model: a message the framework's S-2 gate declined
came back as `status: error` carrying the generic line "No answer could be produced for
this request." `normalize_terminal_output()` raises on any status but SUCCESS, so the
runner discarded the whole envelope and the sender read **"agent failed"** — with nothing
to act on, and no reason to send anything different next time.

| Situation | What is returned | Why |
|---|---|---|
| S-2 declined the MESSAGE | `status: success`, `refusal_kind: "input"`, a sentence naming what to change, plus the trailer | The sender is legitimate and holds something they can fix; they only learn that if the reply reaches them |
| The agent has its own refusal wording | That wording, not the shared sentence | "The shipment could not be classified" says which step stopped; the generic line does not |
| S-1 denied the CALLER | `status: error`, `refusal_kind: "trust"`, the refusal and nothing else | A caller not permitted to invoke the agent must not be told what it is for |
| S-3 blocked the agent's OWN output | unchanged — `status: error` | The agent produced something its output gate would not pass. The sender can do nothing with that, and must not be invited to retry |
| The agent genuinely broke | unchanged — `status: error` | The one signal that says this is an operations problem |

Nothing downstream reads `status` to detect a refusal any more: the envelope names the
refusal in `refusal_kind`. A contract that could only be read by the symptom it was fixing
was not a contract.

Enforced by `tests/unit/test_disclaimer_always_present.py` —
`test_a_refused_MESSAGE_is_delivered_and_says_what_to_change`,
`test_a_REAL_failure_is_still_an_error` (its control), and
`test_the_gate_token_is_matched_as_a_whole_token`.
