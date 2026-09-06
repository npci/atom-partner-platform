# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Background workers fired after an inbound `change_communication`.

WS11 — replaces the old `threading.Thread(daemon=True)` workers. Work is now
scheduled on the running event loop via `asyncio.to_thread` (so blocking DB /
httpx / LLM calls don't block the loop, exceptions are logged, and tasks are
tracked) instead of detached daemon threads. The feasibility step runs through
`registry.get("feasibility").execute(...)`, so every run is recorded in
`agent_runs` — a failed analysis is a visible row, not a swallowed exception.
"""
from __future__ import annotations

import asyncio
import logging

from app.models import IncomingChange

logger = logging.getLogger(__name__)

# Keep strong refs to scheduled tasks so they aren't GC'd mid-flight.
_bg_tasks: set = set()


def _guard(fn, *args) -> None:
    try:
        fn(*args)
    except Exception:  # noqa: BLE001
        logger.exception("background worker %s failed", getattr(fn, "__name__", fn))


async def _guard_async(fn, *args) -> None:
    try:
        await fn(*args)
    except Exception:  # noqa: BLE001
        logger.exception("background worker %s failed", getattr(fn, "__name__", fn))


def _spawn(fn, *args) -> None:
    """Schedule a background worker without a raw daemon thread.

    On the event loop (the inbound A2A path): a SYNC worker runs via
    `asyncio.to_thread` — its blocking DB / httpx / LLM work belongs off the
    loop; a COROUTINE FUNCTION is scheduled on the loop directly (ITA-3),
    which is what lets a worker await the async A2A sender natively. The sync
    workers still send fine: `npci_client._run_portably` detects their
    `to_thread` context (no loop affiliation, per-call client) and runs the
    sender in isolation there.

    With no running loop (e.g. a direct unit-test call), skip it — tests
    invoke the worker explicitly when they want to exercise it. Logged at
    WARNING because outside tests a missing loop means the auto-ack /
    cert-status / feasibility step was dropped silently — worth surfacing.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(
            "no running event loop — not scheduling background %s",
            getattr(fn, "__name__", fn),
        )
        return
    if asyncio.iscoroutinefunction(fn):
        task = loop.create_task(_guard_async(fn, *args))
    else:
        task = loop.create_task(asyncio.to_thread(_guard, fn, *args))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def schedule_post_receive(
    npci_change_id: str,
    local_change_id: str,
    kit_id: str | None,
    version_received: int,
    in_response_to: str | None,
    kit_files_received: list[dict],
) -> None:
    """Schedule the post-receipt steps: auto-ack and the feasibility analysis.
    Each opens its own DB session.

    NOTE: we deliberately do NOT auto-emit cert_status_update(received) here.
    Receipt belongs to the change lifecycle, not the cert lifecycle — NPCI's
    dispatch already moves the assignment to RECEIVED, and proposal_acknowledged
    is the receipt confirmation. Firing a cert_status_update at receipt time
    conflated the two lifecycles and was redundant. The cert lifecycle
    (cert_status_update deployed/tested/ready_for_certification) starts when the
    partner actually progresses toward certification."""
    _spawn(
        _auto_ack, npci_change_id, local_change_id, kit_id,
        version_received, in_response_to, kit_files_received,
    )
    _spawn(_auto_feasibility, local_change_id)


def _auto_cert_status(npci_change_id: str, local_change_id: str, status: str) -> None:
    """Auto-emit CERT_STATUS_UPDATE. Stamps cert_status_history with the
    transition timestamp on the local row regardless of A2A success — the
    partner UI needs the timestamp even if the wire is temporarily broken."""
    import json as _json
    from datetime import datetime, timezone

    from app.database import SessionLocal
    from app.npci_client import send_cert_status_update

    bg_db = SessionLocal()
    try:
        row = bg_db.get(IncomingChange, local_change_id)
        if row:
            history = {}
            if row.cert_status_history:
                try:
                    history = _json.loads(row.cert_status_history) or {}
                except Exception:
                    history = {}
            history[status] = datetime.now(timezone.utc).isoformat()
            row.cert_status = status
            row.cert_status_history = _json.dumps(history)
            bg_db.commit()
        try:
            send_cert_status_update(bg_db, npci_change_id, status)
            logger.info(
                "Auto cert_status_update sent: change=%s status=%s",
                npci_change_id, status,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Auto cert_status_update failed (local row updated anyway)", exc_info=True)
    finally:
        bg_db.close()


def _auto_ack(
    npci_change_id: str,
    local_change_id: str,
    kit_id: str | None,
    version_received: int,
    in_response_to: str | None,
    kit_files_received: list[dict],
) -> None:
    """Auto-emit PROPOSAL_ACKNOWLEDGED and flip decision='acknowledged' on
    success. Opens a fresh session; all failures caught and logged."""
    from app.database import SessionLocal
    from app.npci_client import send_proposal_acknowledged

    bg_db = SessionLocal()
    try:
        result = send_proposal_acknowledged(
            bg_db, npci_change_id, kit_id, version_received,
            in_response_to, kit_files_received,
        )
        if result:
            row = bg_db.get(IncomingChange, local_change_id)
            if row:
                row.decision = "acknowledged"
                bg_db.commit()
                logger.info(
                    "Auto-ack sent: change=%s files=%d",
                    local_change_id, len(kit_files_received),
                )
        else:
            logger.warning(
                "Auto-ack returned None: change=%s — decision stays pending",
                local_change_id,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Auto-ack failed: change=%s", local_change_id)
    finally:
        bg_db.close()


def _auto_feasibility(local_change_id: str) -> None:
    """Run the feasibility agent on inbound change_communication.

    Routes through the agent registry, so the run is audited in `agent_runs`.
    Best-effort: failure is non-fatal and the partner can re-run via the manual
    endpoint. Uses a fresh DB session (the inbound handler's session is closed
    by the executor after the handler returns)."""
    import json as _json

    from sqlalchemy import select

    from app.agents import registry
    from app.database import SessionLocal
    from app.models import ChangeDocument, FeasibilityReport, PartnerSetting

    db = SessionLocal()
    try:
        change = db.get(IncomingChange, local_change_id)
        if change is None:
            logger.warning(
                "Auto-feasibility: change row missing: %s — skipping", local_change_id
            )
            return

        api_key_row = db.execute(
            select(PartnerSetting).where(
                PartnerSetting.key == "partner_anthropic_api_key"
            )
        ).scalar_one_or_none()
        api_key = api_key_row.value if api_key_row and api_key_row.value else None

        # Version-aware context: v1 baseline + LLM summary of later-version
        # changes (option C), instead of every revision the partner holds.
        from app.agents.revision_context import assemble_change_context
        ctx = assemble_change_context(db, local_change_id, api_key)
        if not ctx["documents"]:
            logger.info(
                "Auto-feasibility: no documents on %s — skipping", local_change_id
            )
            return

        agent_input = {
            "change_title": change.title,
            "change_initial_prompt": change.initial_prompt,
            "change_enhanced_prompt": change.enhanced_prompt,
            "documents": ctx["documents"],
            "revision_summary": ctx["revision_summary"],
            "api_key": api_key,
            "change_id": local_change_id,
        }

        try:
            report = registry.get("feasibility").execute(
                agent_input, db=db, change_id=local_change_id
            )
        except Exception as exc:  # noqa: BLE001 — already audited as a failed run
            logger.warning("Auto-feasibility: agent run failed for %s", local_change_id, exc_info=True)
            return

        prior = db.execute(
            select(FeasibilityReport.version).where(
                FeasibilityReport.change_id == local_change_id
            )
        ).scalars().all()
        next_version = (max(prior) + 1) if prior else 1

        meta = report.get("_meta", {})
        row = FeasibilityReport(
            change_id=local_change_id,
            version=next_version,
            content=_json.dumps(report),
            profile_version=meta.get("profile_version"),
            model_used=meta.get("model_used"),
        )
        db.add(row)
        db.commit()
        logger.info(
            "Auto-feasibility: report v%s stored for %s posture=%s",
            next_version, local_change_id, report.get("overall_posture"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Auto-feasibility failed for %s", local_change_id)
    finally:
        db.close()
