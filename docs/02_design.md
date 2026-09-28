# 02 — Design Specification: CMN-C1-596 Enterprise Stripe Refund Process Agent

## Position in AgentCore Architecture

- **Agent Class**: `StripeRefundProcessAgent` (graph class `Graph`)
- **L1 Base**: `AgentBaseGraph` (L1 direct). Not `AutonomousBaseGraph` — the pipeline
  is fixed and deterministic, not a self-directed think-act loop.
- **Three-Layer Separation**:
  - **State**: flat `TypedDict` (`State(AgentState)`); compound values JSON-serialized
    to `str` so the checkpoint stays msgpack-safe (no Pydantic/dataclass).
  - **Node**: L1 inheritance via `FunctionNode`; override `execute(self, state) -> dict`
    only (Template Method). No `__call__` / `_security_gate_*` override.
  - **Graph**: composition — `register_nodes()` (constructor-DI services) + `add_edges()`.

## Capability

Initiate **one** Stripe refund from a natural-language instruction, behind a
verify → compute → invoice-check → **confirmation gate** → audited-write envelope.
Single write endpoint: `POST /v1/refunds`.

## Node Configuration

The framework's `compile()` requires the slots `pre_process` / `main` / `post_process`.
The two read nodes and the compute node are registered as extra nodes between
`pre_process` and the `main` writer slot; `add_edges()` wires them linearly.

| Node | Slot | Base | Responsibility | Writes |
|------|------|------|----------------|--------|
| initialize | (framework) | InitializeNode | framework init | — |
| pre_process | pre_process | FunctionNode | **IntentParse** — parse EN/JA instruction → intent; S-2 input gate | `parsed_intent`, `target_context`, `validation_error`, `output_language` |
| charge_verify | extra | FunctionNode | **ChargeVerify** — `GET /v1/charges/{id}` (read) | `charge_facts` |
| refund_calc | extra | FunctionNode | **RefundAmountCalc** — compute refundable amount; reject over-refund / already-refunded | `refund_plan`, `validation_error` |
| invoice_check | extra | FunctionNode | **InvoiceCheck** — Japan qualified-invoice impact (read-only, never blocks) | `invoice_notice` |
| main | main | FunctionNode | **RefundExecute** — confirmation gate + idempotency + single `POST /v1/refunds` | `refund_result`, `validation_error` |
| post_process | post_process | FunctionNode | **ConfirmationGenerate** — EN/JA report; S-3 egress redaction | `confirmation_report`, `formatted_output` |
| finalize | (framework) | FinalizeNode | framework finalize | — |

### Data Flow

```
START → initialize → pre_process → charge_verify → refund_calc → invoice_check
      → main → {route} → post_process → finalize → END
```

Every domain node short-circuits when `status == ERROR` or `validation_error` is set,
so a bad input flows to `post_process` as a refusal report without any write.

## State Definition

`State(AgentState)` — inherited framework fields (`user_input`, `validated_input`,
`status`, `session_id`, `node_history`, `error_log`, `formatted_output`, `result`,
`caller_trust_level`, `input_context`, `hitl_*`) are **not** redeclared.

| Field | Type | Encoding | Purpose | Writer |
|-------|------|----------|---------|--------|
| `parsed_intent` | `str` | JSON `{charge_id, payment_intent, amount, reason, case_ref, confirm, dry_run}` | parsed instruction | pre_process |
| `target_context` | `str` | JSON `{charge_id, payment_intent, confirm, dry_run, case_ref}` | write parameters | pre_process |
| `validation_error` | `str` | plain (`""` = none) | business-invalid signal; short-circuits downstream | pre_process / refund_calc / main |
| `output_language` | `str` | `"en"`\|`"ja"`\|`"bilingual"` | render language | pre_process |
| `charge_facts` | `str` | JSON `{found, amount, amount_refunded, currency, refunded, status, qualified_invoice_issued}` | verified charge | charge_verify |
| `refund_plan` | `str` | JSON `{amount, currency, reason, is_full, high_impact}` | computed refund | refund_calc |
| `invoice_notice` | `str` | plain | Japan qualified-invoice reminder (may be empty) | invoice_check |
| `refund_result` | `str` | JSON `{executed, dry_run, needs_confirmation, refund_id, charge_id, amount, reason?}` | write outcome | main |
| `confirmation_report` | `str` | Markdown | final report; mirrored into `formatted_output` | post_process |

`formatted_output` (inherited) carries the same report and is what `get_output()`
surfaces — the outer graph does **not** override `get_output()`.

**State constraints (mandatory):**
- Flat TypedDict; primitives + JSON `str` only.
- No API key / credential in State — the Stripe secret is fetched per call via
  `current_secrets().require("STRIPE_API_KEY")` and never persisted.
- `InvocationContext` via `InvocationContext.from_state(state)` / `config["configurable"]`,
  never stored in State.
- No Pydantic / dataclass / arbitrary objects.

## Stripe REST contract (verified against docs.stripe.com, 2026-07-08)

| Operation | Verb + path | Notes |
|-----------|-------------|-------|
| Verify charge | `GET  https://api.stripe.com/v1/charges/{id}` | returns `amount`, `amount_refunded`, `currency`, `refunded`, `status`, `metadata` |
| Create refund | `POST https://api.stripe.com/v1/refunds` | form-encoded body `charge` **or** `payment_intent`, `amount` (integer, minor units), `reason` ∈ {`duplicate`,`fraudulent`,`requested_by_customer`} |
| Auth | `Authorization: Bearer <STRIPE_API_KEY>` | secret key; per-call, never cached |
| Idempotency | `Idempotency-Key: <key>` header (POST only) | deterministic key = `sha256(charge|amount|case_ref)` so a retry returns the original refund, never a second one |
| Egress | host must be `api.stripe.com` | S-3 egress guard rejects any other host |

`amount` is in the smallest currency unit; for zero-decimal currencies such as JPY the
integer *is* the yen amount. Refundable remaining = `amount - amount_refunded`.

## Confirmation gate + idempotency (core safety design)

A refund is high-impact, irreversible money movement, so **every** refund requires an
explicit confirmation in the instruction:

- `confirm == false` and not `dry_run` → `needs_confirmation` preview, **no write**.
- `dry_run == true` → preview only, **no write**.
- upstream `ERROR` / `validation_error` / no `refund_plan` → **no write**.
- otherwise → single `POST /v1/refunds` with a deterministic `Idempotency-Key`.

The idempotency key is derived from `charge_id | amount | case_ref`, so if the caller
retries the same logical refund, Stripe returns the original refund instead of creating
a second one.

## 5-Layer Security Mapping

| Layer | Where | Implementation |
|-------|-------|----------------|
| S-1 Trust gate | every domain node | `required_trust_level = VERIFIED_EXTERNAL` (ClassVar). An ANONYMOUS caller is refused → status ERROR (no write). |
| S-2 Input gate | pre_process | framework `@final _security_gate_input` (PII scan) + `_extra_security_gate_input` (unsafe-char / size → ERROR, never raise). The instruction is read from `input_context["instruction"]` (structured channel, **not** in `_PII_SCAN_FIELDS`) so a charge id / case ref survives the name-masking heuristic; fallback to `user_input`. |
| S-3 Output gate | post_process | `_extra_security_gate_output` redacts any Stripe key shape (`sk_…` / `rk_…` / long opaque token). Graph-level S-3 is dead (`AgentBaseGraph._extra_security_gate_output` absent on the wheel) → S-3 lives on the node. |
| S-4 Audit | every node | `emit_trace_event(event_type, payload, state)` from `shared.utils.audit_logger`, ≥1 domain event per node. Backbone `node_start`/`node_complete`/`node_error` are framework-emitted — not duplicated. |
| S-5 Credentials | services + CI | `STRIPE_API_KEY` via `current_secrets().require()`; declared in `agent.yaml requires.secrets`; never in State/source. `gate-credential-scan` at CI. |

## Edge cases

| Input | Behaviour |
|-------|-----------|
| No charge / payment_intent parsed | graceful `validation_error` (SUCCESS) → refusal report, no write |
| Charge not found / API read error | `validation_error` → refusal, no write |
| Requested amount > refundable remaining | over-refund blocked → refusal, no write |
| Charge already fully refunded (remaining 0) | blocked → "already refunded", no write |
| High-impact request without explicit confirm | `needs_confirmation` preview, no write |
| `dry_run` | preview only, no write |
| Refund API write error | surfaced as not-executed report (SUCCESS, no crash) |
| Unsafe input (path/control chars, oversize) | S-2 hard reject → status ERROR |
| Japanese input | NFKC-normalized; JA output with 用語統制 + keigo + 認証された翻訳ではありません |

The agent never fabricates a refund outcome and never writes on any refusal branch.

## Japan qualified-invoice (インボイス制度) rule

If the charge currency is JPY and a qualified invoice (適格請求書) was issued
(`metadata.qualified_invoice_issued == "true"`), the report warns that a qualified
refund invoice (適格返還請求書) must be issued. When the flag is **absent**, a
**conservative** reminder is returned (per HuyVV7 design note). Non-JPY charges get no
invoice notice. This notice is read-only and never blocks the write.

## Framework Utilization

- `InvocationContext` (session_id, caller_trust_level, caller_id) — via `ctx` / `from_state`.
- `emit_trace_event` — S-4 audit.
- `current_secrets().require()` / `bound_secrets` — secret binding.
- S-2 `_extra_security_gate_input()` / S-3 `_extra_security_gate_output()` hooks
  (never override the `@final` `_security_gate_*`).

## Import Isolation

- No `agenticstar` (Level 0) import; no `framework.security` / `framework.llm`
  (ci_stub-only, absent on the wheel).
- Imports limited to `framework.*` (public) + `shared.*` + third-party `requests`.

## Composition Pattern

- **Pattern**: Standalone Cat 1 (no GraphNode / RemoteAgentNode). Single write node.
- **Error propagation**: business-invalid → `validation_error` carried to post_process
  as a refusal (status SUCCESS); genuinely unsafe input → status ERROR. No exception
  crosses the graph boundary; the write node surfaces API errors as a not-executed report.

## HITL / memory

Not enabled (`hitl.enabled` absent). The confirmation gate is satisfied by an explicit
confirmation token in the instruction, not by a backbone `interrupt()` — so no
checkpointer is required.

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | **AgentBaseGraph** | fixed deterministic pipeline, no LLM loop |
| Composition | Standalone (extra nodes) | GraphNode subgraph | **Standalone** | Cat 1 single capability — `main` is one FunctionNode |
| Confirmation | explicit token in instruction | backbone `interrupt()` HITL | **explicit token** | avoids mandatory checkpointer; caller supplies confirm/dry-run |
| Instruction channel | `input_context["instruction"]` | `user_input` only | **input_context (fallback user_input)** | charge id / case ref survive the S-2 name-mask heuristic |
| Idempotency key | deterministic `sha256(charge\|amount\|case_ref)` | random UUID per call | **deterministic** | a retry of the same logical refund never double-refunds |
| Refund confirmation | always required | only when > threshold | **always** | any refund is irreversible money movement |

## Known constraint — write modes on the Marketplace one-shot Pod entry point (2026-09-14)

`run_agent_marketplace()` stamps every caller `VERIFIED_EXTERNAL` and offers no surface to
raise it. CoE ruled (an internal ruling, the framework contract; tracked at (internal reference removed) / #251) that write
modes are **out of scope for this entry point**, and that lowering a template's own write
authorization to `VERIFIED_EXTERNAL` so they become reachable is **prohibited**.

That matters here more than in most templates: `RefundExecuteNode` issues a real refund
against the customer's Stripe account, and the `confirm` flag it checks is a field in the
caller's own payload — a guard against accident, not an authorisation. At
`VERIFIED_EXTERNAL` any Marketplace user could refund any charge the agent can resolve.

**Present state.** The template deploys and registers normally. `RefundExecuteNode`
declares `required_trust_level = TrustLevel.INTERNAL`, and `Graph.add_edges()` routes
`invoice_check → post_process` for a caller who cannot reach that level, so the refund node
never runs. The caller is told, in their own language, that the refund was not performed
and where it can be performed — at `status: success`, because the runner raises on
anything else and they would otherwise be shown "agent failed" for a request that was
merely not permitted. Everything up to the refund still runs: the charge is verified, the
amount computed, the qualified-invoice impact noted. It is a read-only answer, which is
what Stage ⑤ accepts.

The supported deployment for the write path is the standalone HTTP entry point
(`src/api/server.py`), whose adapter maps an authenticated runner credential to `INTERNAL`.

Verified 2026-09-14 with the real model: a Marketplace caller gets the refusal and zero
Stripe calls; an INTERNAL caller gets the refund and the confirmation report.

This is a **present-state constraint, not a permanent exclusion** — (internal reference removed) is open.

