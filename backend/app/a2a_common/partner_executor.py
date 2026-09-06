# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Partner platform `AgentExecutor` — JSON-RPC server-side dispatcher.

Receives an A2A `Task` from NPCI, reads `task_type` + `payload` from
the message's data Part, and dispatches to the existing handler
functions in `app.api.a2a` (`_handle_change_communication`,
`_handle_clarification_response`).

Zero business logic is duplicated here — Slice 8 will move the handler
bodies into this file once the legacy router is decommissioned.

Message data shape (NPCI → partner):
    {
      "task_type":  "change_communication",   // or "clarification_response"
      "change_id":  "<npci-side uuid>",       // optional per task type
      "payload":    {...}                     // task-specific body
    }

Auth note (Slice 4): the partner side's legacy `POST /api/a2a/tasks/send`
has no auth at all today (relies on network ACLs). The SDK path
preserves that — Slice 5/6 generalises Bearer JWT validation across
both backends.

Outbound task types (`query`, `progress`, `readiness`) are NOT
dispatched here — they're partner→NPCI and ride out via
`app.a2a_common.client.send_a2a_message`. If NPCI ever sends one of
those to the partner by mistake, we acknowledge but no-op (matches
legacy behaviour).
"""
from __future__ import annotations

import logging
from typing import Any

from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types.a2a_pb2 import Part
from google.protobuf import json_format, struct_pb2
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# Inbound types the partner actually processes. Anything else gets a
# generic acceptance so NPCI's outbound client can wait for a response
# even when the partner doesn't have a real handler — matches the
# legacy router's fall-through behaviour.
_INBOUND_TASK_TYPES = {
    "change_communication",
    "clarification_response",
    "counter_decision",
    "revision_in_progress",
    "negotiation_frozen",
    "round_opened",
    "round_closed",
    "blocker_resolution",
    "blocker_status_update",
    "cert_execution_start",
    "cert_signoff_notification",
    "cert_completion_signoff",
    # Dispatched since WS8 but never listed here — same omission class as the
    # cert_lifecycle types below, found by test_cert_task_types_are_recognised_inbound.
    "cert_test_response",
    # Richly handled by handlers/cert_lifecycle.py — the bank replies with its own
    # config, per-case test data, waiver request and (for bank-initiated cases) a
    # formal result report, instead of a bare ack.
    #
    # These were wired in 68cd0a44 and silently dropped again by 33dc4a9e (a
    # round_opened/round_closed change that removed them in a merge). While
    # unwired the bank contributed NOTHING to the on-screen conversation for 30
    # cert_case_result messages — see HANDLER_TASK_TYPES below for the guard that
    # now makes that failure loud.
    "cert_config_request",
    "cert_setup_notification",
    "cert_case_result",
    "cert_verdict_notification",
    # Integration-testing tunnel (ITA I-1). Must be listed here as well as in
    # HANDLER_TASK_TYPES: a type that is dispatched but not RECOGNISED falls
    # through to the generic ack branch, which reports success while doing
    # nothing — the exact misreport
    # test_cert_task_types_are_recognised_inbound exists to catch.
    "http_exchange_request",
    # Recognised but not yet richly handled — accepted with a no-op ack; full
    # handling (status reports, waiver-decision UI, etc.) lands with the Phase 6
    # cert UI that consumes them.
    "milestone_status_request",
    "milestone_status_report",
    "cert_waiver_decision",
    "cert_status_request",
    "cert_status_report",
}


# Every task type that MUST resolve to a real handler. Declared as plain strings
# so it costs no imports and can be asserted against without standing up the app.
#
# `build_handler_registry()` is checked against this by
# tests/test_handlers.py::test_every_declared_task_type_has_a_handler. That test
# exists because the cert entries were deleted twice over by unrelated merges and
# nothing failed — the executor just degraded to generic acks, which look like
# success at every layer except the conversation the user is watching.
HANDLER_TASK_TYPES = frozenset({
    "change_communication",
    "clarification_response",
    "counter_decision",
    "blocker_resolution",
    "blocker_status_update",
    "cert_test_response",
    "cert_signoff_notification",
    "cert_completion_signoff",
    "cert_case_result",
    "cert_config_request",
    "cert_setup_notification",
    "cert_verdict_notification",
    "negotiation_frozen",
    "revision_in_progress",
    "round_opened",
    "round_closed",
    # Integration-testing tunnel (ITA I-1). Dispatched OFF the event loop —
    # see the `run_in_thread` branch in execute().
    "http_exchange_request",
    # ITA I-6: the authority's start signal for the partner-initiated half.
    "cert_execution_start",
})


def build_handler_registry() -> dict:
    """task_type → handler. Built on demand, not at import.

    The imports stay inside the function for the same reason they always did:
    `partner_executor` must import cleanly in the test harness without dragging
    in the whole FastAPI app. Extracting the dict here (rather than inlining it
    in `execute()`) is what makes the registry inspectable by a test.
    """
    from app.a2a_common.handlers import (
        handle_blocker_resolution,
        handle_blocker_status_update,
        handle_cert_case_result,
        handle_cert_completion_signoff,
        handle_cert_config_request,
        handle_cert_execution_start,
        handle_cert_setup_notification,
        handle_cert_test_response,
        handle_cert_verdict_notification,
        handle_change_communication,
        handle_clarification_response,
        handle_counter_decision,
        handle_negotiation_frozen,
        handle_revision_in_progress,
        handle_round_closed,
        handle_round_opened,
    )
    from app.services.integration_testing.egress import handle_http_exchange_request

    return {
        "change_communication":      handle_change_communication,
        "clarification_response":    handle_clarification_response,
        "counter_decision":          handle_counter_decision,
        "blocker_resolution":        handle_blocker_resolution,
        "blocker_status_update":     handle_blocker_status_update,
        "cert_test_response":        handle_cert_test_response,
        # cert_completion_signoff and cert_signoff_notification both map to the
        # same handler (protocol v1 §7.11 rename, both accepted in transition).
        "cert_signoff_notification": handle_cert_completion_signoff,
        "cert_completion_signoff":   handle_cert_completion_signoff,
        # Cert lifecycle — the bank's replies in the A2A cert conversation.
        "cert_case_result":          handle_cert_case_result,
        "cert_config_request":       handle_cert_config_request,
        "cert_setup_notification":   handle_cert_setup_notification,
        "cert_execution_start":      handle_cert_execution_start,
        "cert_verdict_notification": handle_cert_verdict_notification,
        "negotiation_frozen":        handle_negotiation_frozen,
        "revision_in_progress":      handle_revision_in_progress,
        "round_opened":              handle_round_opened,
        "round_closed":              handle_round_closed,
        "http_exchange_request":     handle_http_exchange_request,
    }


def _part_to_dict(part: Part) -> dict:
    """Decode a structured A2A `Part` back to a Python dict."""
    if part.HasField("data") and part.data.HasField("struct_value"):
        return json_format.MessageToDict(part.data.struct_value)
    return {}


def _dict_to_part(payload: dict) -> Part:
    """Wrap a Python dict as a structured A2A `Part`."""
    s = struct_pb2.Struct()
    json_format.ParseDict(payload, s)
    v = struct_pb2.Value()
    v.struct_value.CopyFrom(s)
    part = Part()
    part.data.CopyFrom(v)
    return part


class PartnerAgentExecutor(AgentExecutor):
    """Dispatch incoming A2A Tasks to the partner platform's existing handlers.

    One executor instance is shared across all requests by the SDK; do
    not store per-request state on `self`. The DB session is opened
    fresh inside `execute()` and closed in the `finally` block.
    """

    async def execute(
        self, context: RequestContext, event_queue: EventQueue,
    ) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.start_work()

        # ── 1. parse incoming message ──
        data = self._extract_data(context)
        if not data:
            await self._fail(updater, "Empty or malformed message data")
            return

        # Phase 1 (protocol v1): tolerant envelope read — legacy messages
        # with no message_id/correlation_id still parse.
        from app.a2a_common.protocol import read_envelope
        env = read_envelope(data)
        task_type = env.task_type
        change_id = env.change_id
        payload = env.payload

        if not task_type:
            await self._fail(updater, "Missing 'task_type' in message data")
            return

        # ── 2. dispatch ──
        # Imports are inside the function so this module imports cleanly
        # in the test harness without dragging the whole FastAPI app.
        # Slice 8 — handler bodies live in `partner_handlers`; the
        # legacy `app.api.a2a` is deleted.
        from app.a2a_common.handlers import TaskReceiveRequest
        from app.database import SessionLocal

        db: Session | None = None
        try:
            db = SessionLocal()

            # Re-pack into the legacy `TaskReceiveRequest` so we can
            # call the existing handlers verbatim. Slice 8 will inline
            # the handler bodies and drop this re-pack.
            body = TaskReceiveRequest(
                task_type=task_type,
                change_id=change_id,
                payload=payload,
                task_id=context.task_id,
                message_id=env.message_id,
                correlation_id=env.correlation_id,
            )

            logger.info(
                "A2A SDK task received: type=%s change=%s msg_id=%s corr=%s proto=%s",
                task_type, change_id, env.message_id, env.correlation_id,
                env.protocol_version,
            )

            # Dispatch via a task_type → handler registry (replaces the
            # if/elif chain). See build_handler_registry() above.
            handlers = build_handler_registry()
            handler = handlers.get(task_type)
            if handler is not None and payload:
                if getattr(handler, "run_in_thread", False):
                    # A handler that declares `run_in_thread` blocks for longer
                    # than a request should hold the event loop — the tunnel
                    # egress makes an HTTP call that may legitimately take 60s
                    # (ITA §6). Running it inline freezes this platform for the
                    # duration; with several cert cases in flight it stops
                    # answering anything at all.
                    #
                    # Thread dispatch rather than an `async def` handler,
                    # because making handlers async requires the outbound
                    # sender to be async first (ITA-3): an async handler that
                    # reaches today's `npci_client` hits `asyncio.run` inside a
                    # running loop and raises. Revisit when ITA-3 lands.
                    import anyio

                    result = await anyio.to_thread.run_sync(handler, body, db)
                else:
                    result = handler(body, db)
            elif task_type in _INBOUND_TASK_TYPES:
                # Recognised type but missing payload — accept and no-op.
                result = {"status": "accepted", "message": f"Task type '{task_type}' received without payload"}
            else:
                # Unknown / outbound type sent inbound by mistake —
                # acknowledge but no-op (legacy fall-through).
                result = {"status": "accepted", "message": f"Task type '{task_type}' received"}

            # ── 3. emit response artifact ──
            await updater.add_artifact(
                parts=[_dict_to_part(result)],
                name="a2a-task-receipt",
                last_chunk=True,
            )
            await updater.complete()

        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "partner_a2a_executor_error task_type=%s change=%s",
                task_type, change_id,
            )
            await self._fail(updater, f"Executor error: {exc}")
        finally:
            if db is not None:
                db.close()

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue,
    ) -> None:
        """Partner handlers complete synchronously today; cancellation
        mid-execute is a no-op. Wired properly when long-running task
        types land."""
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()

    # ── private ────────────────────────────────────────────────────────────

    def _extract_data(self, context: RequestContext) -> dict[str, Any]:
        """Pull the first structured Part out of the incoming message."""
        if context.message is None:
            return {}
        for part in context.message.parts:
            if part.HasField("data"):
                return _part_to_dict(part)
        return {}

    async def _fail(self, updater: TaskUpdater, reason: str) -> None:
        """Mark the Task FAILED with a human-readable reason artifact."""
        await updater.add_artifact(
            parts=[_dict_to_part({"error": reason})],
            name="a2a-task-error",
            last_chunk=True,
        )
        await updater.failed()
