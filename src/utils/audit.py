"""S-4 audit wrapper. Byte-identical in every template -- the id belongs in the events,
not in this file's header.

Prefers the platform audit logger; falls back to a stderr JSON-Lines record so audit is
never lost and never raises. Never logs raw PII values -- only structured, already-safe
fields.

Two failures this file exists to prevent, both measured on real repositories 2026-08-28,
both of which left every test green and every gate passing:

* **The wrong module name.** (internal reference removed) imported ``shared.utils.trace``, which has never
  existed -- the supported shim is ``shared.utils.audit_logger``. The import raised, a
  broad ``except`` caught it, and all 36 call sites in the agent wrote to stderr instead.
  Verified by attaching a handler to ``agentcore.audit``: it received nothing.
* **A short call, or a None state.** The platform function takes
  ``(event_type, payload, state)`` and reads ``state.get(...)``. Passing two arguments,
  or passing ``None`` through, raises straight into the same broad ``except`` -- the
  events are dropped exactly as if the module were missing.

stderr is not equivalent to the logger. The platform record carries ``trace_id``,
``correlation_id`` and ``session_id`` read from state; those are what tie an audit line
back to the request that produced it and what the platform ships to its aggregator.
Without them there are logs, but not evidence.

"Best-effort / never raises" must NOT mean "swallow silently": a dropped audit event is
itself the security finding (ESC-FIN-028-029-S4). Every failure is therefore marked with
``audit_degraded`` on the fallback record, with exactly one quiet case -- the platform
logger module being absent, which is the expected stub-harness-vs-wheel difference and not a
defect.
"""

from __future__ import annotations

import json
import sys
from typing import Any

_PLATFORM_LOGGER = "shared.utils.audit_logger"


def _fallback(
    event_type: str,
    payload: dict[str, Any],
    state: dict[str, Any] | None,
    degraded_reason: str | None = None,
) -> None:
    """Last-resort stderr JSON-Lines record. Never raises."""
    try:
        record: dict[str, Any] = {"event": event_type, **payload}
        if state is not None:
            record["correlation_id"] = state.get("correlation_id", "")
        if degraded_reason is not None:
            record["audit_degraded"] = degraded_reason
        sys.stderr.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001 -- audit must never break the request path
        pass


def emit_trace_event(
    event_type: str,
    payload: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    """Emit a structured S-4 audit event. Best-effort, never raises.

    Note the wording above avoids writing the literal call form with a backbone event
    name inside it: repo-side scanners grep the raw source for
    ``emit_trace_event`` immediately followed by "node_start"/"node_complete"/..., and a
    prose example matched it -- this shared file made a correct repo fail its own S-4
    compliance test (measured on (internal reference removed), 2026-09-03). A comment is not a call, but a
    grep cannot tell.

    ``payload`` is optional, unlike the platform function this wraps: proof-of-boundary
    tests assert that calling this with only an event name does not raise, and that
    assertion is the point -- audit must never break the request path, including when a
    caller omits an argument. It is normalised before it goes anywhere.
    """
    payload = payload if payload is not None else {}
    try:
        from shared.utils.audit_logger import emit_trace_event as _platform_emit
    except ModuleNotFoundError as exc:
        # Quiet ONLY when the logger module itself (or a parent package) is absent.
        if exc.name is not None and (_PLATFORM_LOGGER == exc.name or _PLATFORM_LOGGER.startswith(exc.name + ".")):
            _fallback(event_type, payload, state)
        else:
            # A dependency of the logger is missing -- a real gap, not the ci_stub case.
            _fallback(event_type, payload, state, degraded_reason=type(exc).__name__)
        return
    except ImportError as exc:  # module present but fails to import
        _fallback(event_type, payload, state, degraded_reason=type(exc).__name__)
        return

    try:
        _platform_emit(event_type, payload, state if state is not None else {})
    except Exception as exc:  # noqa: BLE001 -- never break flow, but never hide the gap
        _fallback(event_type, payload, state, degraded_reason=type(exc).__name__)
