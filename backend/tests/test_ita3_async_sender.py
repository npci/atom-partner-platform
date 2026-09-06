# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""ITA-3 (blocker B2): the sender returns the reply and leaves `asyncio.run`.

The I-3 verify bar, as behaviour (no source greps — a grep passed with the
code deleted once):
  * an existing caller that ignores the return is unaffected — success is
    truthy, failure is None and still queues a retry;
  * the body arrives — the receiver's receipt dict comes back through
    `send_task` and `send_task_async`;
  * no event-loop reuse under concurrent sends — thread senders run isolated
    loops, anyio-worker senders share the application's own loop;
  * the ITA-4 enabler: `await send_task_async(...)` works under a running
    loop, and the sync facade REFUSES the loop instead of deadlocking.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

import app.npci_client as npci_client

RECEIPT = {"task_id": "m1", "status": "completed", "task_type": "echo",
           "message": "echo_ok"}


@pytest.fixture
def stub_wire(monkeypatch):
    """Replace the transport core with an async stub that records the loop it
    ran on and returns a receipt."""
    calls = []

    async def fake_dispatch(db, task_type, change_id, payload, *,
                            job_correlation_id=None, idempotency_key=None,
                            timeout=None):
        calls.append({"task_type": task_type,
                      "loop": asyncio.get_running_loop(),
                      "thread": threading.get_ident()})
        return dict(RECEIPT)

    monkeypatch.setattr(npci_client, "_dispatch_wire", fake_dispatch)
    return calls


# ── the body arrives; existing-caller contract preserved ─────────────────────

def test_the_reply_body_comes_home(stub_wire, db_session):
    assert npci_client.send_task(db_session, "echo", None, {}) == RECEIPT


def test_success_without_an_artifact_keeps_the_delivery_marker(monkeypatch, db_session):
    """A receiver that emits no structured artifact still yields the old
    truthy marker — ~30 call sites branch on truthiness."""
    async def fake(db, *a, **k):
        return None

    monkeypatch.setattr(npci_client, "_dispatch_wire", fake)
    assert npci_client.send_task(db_session, "echo", None, {}) == {"status": "delivered"}


def test_transport_failure_returns_none_and_queues_the_retry(monkeypatch, db_session):
    from app.models import OutboundA2ARetry

    async def fake(db, *a, **k):
        raise RuntimeError("wire down")

    monkeypatch.setattr(npci_client, "_dispatch_wire", fake)
    assert npci_client.send_task(db_session, "echo", "chg-1", {"x": 1}) is None
    rows = db_session.query(OutboundA2ARetry).all()
    assert len(rows) == 1 and rows[0].task_type == "echo"


# ── the loop rules ───────────────────────────────────────────────────────────

def test_async_caller_awaits_the_sender_under_a_running_loop(stub_wire, db_session):
    """The ITA-4 enabler: an async handler can await the sender natively —
    exactly what `asyncio.run` inside the old body made impossible."""
    async def scenario():
        return await npci_client.send_task_async(db_session, "echo", None, {})

    assert asyncio.run(scenario()) == RECEIPT


def test_sync_facade_refuses_the_event_loop(stub_wire, db_session):
    """Calling the sync bridge ON the loop would deadlock it — refused loudly,
    naming the async API to use instead."""
    async def scenario():
        npci_client.send_task(db_session, "echo", None, {})

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(scenario())
    assert "send_task_async" in str(exc.value)
    assert stub_wire == [], "nothing may be sent on the refused path"


def test_anyio_worker_sends_ride_the_application_loop(stub_wire, db_session):
    """The `def`-route caller class: inside an anyio worker thread the bridge
    must reach the PARENT loop (from_thread), not spin a private one."""
    import anyio
    from anyio import to_thread

    seen = {}

    async def scenario():
        seen["loop"] = asyncio.get_running_loop()
        return await to_thread.run_sync(
            lambda: npci_client.send_task(db_session, "echo", None, {}))

    assert anyio.run(scenario) == RECEIPT
    assert stub_wire[0]["loop"] is seen["loop"], \
        "an anyio worker must share the application's own event loop"


def test_plain_thread_sends_use_isolated_loops_no_reuse(stub_wire, db_session):
    """The background-thread caller class, concurrently: every send completes,
    every body arrives, and no two sends share an event loop (the old shared-
    state hazard behind the asyncio.run removal)."""
    results: list = [None] * 4

    def worker(i):
        results[i] = npci_client.send_task(db_session, "echo", None, {})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == [RECEIPT] * 4
    loops = [c["loop"] for c in stub_wire]
    assert len(loops) == 4
    for i in range(4):
        for j in range(i + 1, 4):
            assert loops[i] is not loops[j], "two sends shared an event loop"


# ── _spawn schedules coroutine workers directly ──────────────────────────────

def test_spawn_schedules_a_coroutine_on_the_loop_not_a_thread():
    from app.a2a_common.handlers._background import _spawn

    ran = {}

    async def worker(value):
        ran["thread"] = threading.get_ident()
        ran["value"] = value

    async def scenario():
        _spawn(worker, 42)
        await asyncio.sleep(0.05)

    main_thread = threading.get_ident()
    asyncio.run(scenario())
    assert ran["value"] == 42
    assert ran["thread"] == main_thread, \
        "a coroutine worker must run ON the loop, not in a to_thread worker"


def test_spawn_still_dispatches_sync_workers_to_a_thread():
    from app.a2a_common.handlers._background import _spawn

    ran = {}

    def worker():
        ran["thread"] = threading.get_ident()

    async def scenario():
        _spawn(worker)
        await asyncio.sleep(0.05)

    main_thread = threading.get_ident()
    asyncio.run(scenario())
    assert ran["thread"] != main_thread, \
        "a sync worker's blocking work belongs OFF the loop"


# ── the client-layer capture (B2's other half) ───────────────────────────────

def test_extract_artifact_dict_unwraps_a_task_receipt():
    pytest.importorskip("a2a")
    from a2a.types.a2a_pb2 import Artifact, Part, StreamResponse, Task
    from google.protobuf import json_format, struct_pb2

    from app.a2a_common.client import _extract_artifact_dict

    s = struct_pb2.Struct()
    json_format.ParseDict(RECEIPT, s)
    part = Part()
    part.data.struct_value.CopyFrom(s)
    event = StreamResponse(task=Task(artifacts=[Artifact(parts=[part])]))

    assert _extract_artifact_dict(event) == RECEIPT


def test_extract_artifact_dict_returns_none_for_bare_status_events():
    pytest.importorskip("a2a")
    from a2a.types.a2a_pb2 import StreamResponse, Task

    from app.a2a_common.client import _extract_artifact_dict

    assert _extract_artifact_dict(StreamResponse(task=Task())) is None
