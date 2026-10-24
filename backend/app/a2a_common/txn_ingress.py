"""Partner Ingress — A2A txn_* in, switch HTTP out (docs/upi_over_a2a_plan.md, M3 3c).

Mirror of NPCI's `txn_ingress`: runs behind this partner's SDK mount (JWT + HMAC
already verified), checks direction legality and that the configured switch
adapter fronts the named identity, matches the uri against the UPI allow-list,
POSTs the decoded bytes to that switch, and returns the switch's <Ack> inside the
reply. Never authors or fabricates an ACK; a failure is a named fault.

Which switch an identity maps to is the adapter's business (`switch_base`), so a
partner's own switch plugs in without touching this module.
"""
from __future__ import annotations

import base64
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from app.a2a_common import txn_state

logger = logging.getLogger("partner.txn")

# Same shape as the NPCI backend adapter's allow-list (txn_adapter.py); duplicated
# because the partner deploys standalone on the bank's side.
_UPI_URI_RE = re.compile(
    r"^/upi/[A-Za-z][A-Za-z0-9]{2,40}/[0-9][0-9A-Za-z.\-]{0,15}/urn:txnid:[0-9A-Za-z.\-_]{1,64}$"
)

# Inbound to the BANK, the legal (task_type, initiated_by) combinations are
# the NPCI→bank half of the direction map.
_LEGAL_INBOUND = {("txn_request", "NPCI"), ("txn_response", "BANK")}


def _fault(message_id: str, correlation: str | None, code: str, detail: str) -> dict:
    return {"kind": "txn_ack", "acks_message_id": message_id,
            "correlation_id": correlation,
            "fault": {"fault_code": code, "fault_detail": detail}}


async def process_inbound_txn(data: dict) -> dict[str, Any]:
    """Full inbound envelope dict → the TxnReply dict the executor emits."""
    import httpx

    from app.adapters import get_switch_adapter
    from app.config import settings

    message_id = str(data.get("message_id") or "")
    if not message_id:
        raise ValueError("txn_* requires a message_id (dedup key)")

    stored = txn_state.stored_reply(message_id)
    if stored is not None:
        logger.info("txn_ingress: duplicate message_id=%s — replaying stored reply",
                    message_id)
        return stored

    task_type = str(data.get("task_type") or "")
    payload = data.get("payload") or {}
    initiated_by = str(payload.get("initiated_by") or "")
    cert = payload.get("certification") or {}
    txn = payload.get("txn") or {}
    meta = txn.get("metadata") or {}
    header = txn.get("header") or {}
    product = str(cert.get("product") or "")
    org_id = str(cert.get("org_id") or "")
    uri = str(meta.get("uri") or "")
    txn_ref = str(meta.get("txn_ref") or "")
    cflow_id = data.get("cflow_id") or txn_state.cflow_for(product, org_id) or ""
    correlation = data.get("correlation_id") or (
        txn_state.correlation_for(cflow_id, txn_ref) if cflow_id and txn_ref else None)

    def refused(code: str, detail: str) -> dict:
        txn_state.record_hop(message_id=message_id, direction="incoming",
                             task_type=task_type, initiated_by=initiated_by,
                             correlation_id=correlation, cflow_id=cflow_id,
                             txn_ref=txn_ref, product=product, uri=uri or None,
                             fault_code=code, fault_detail=detail)
        reply = _fault(message_id, correlation, code, detail)
        txn_state.remember_reply(message_id, reply)
        return reply

    if (task_type, initiated_by) not in _LEGAL_INBOUND:
        return refused("WRONG_DIRECTION",
                       f"{task_type} with initiated_by={initiated_by or '?'} is not "
                       "an NPCI→bank message")
    if not product or not org_id:
        raise ValueError("certification.product and certification.org_id are required")

    base = get_switch_adapter().switch_base(product, org_id)
    if base is None:
        return refused("UNKNOWN_PARTNER",
                       f"this partner fronts no switch for {product}/{org_id}")
    base = base.rstrip("/")

    if product != "upi" or not _UPI_URI_RE.match(uri):
        return refused("UNKNOWN_TARGET", f"uri not on the {product or '?'} allow-list")

    try:
        raw = base64.b64decode(txn.get("txn_payload") or "", validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"txn_payload is not valid base64: {exc}") from exc

    seq = 1 if task_type == "txn_request" else 3
    content_type = header.get("content_type") or "application/xml"
    txn_state.record_hop(message_id=message_id, direction="incoming",
                         task_type=task_type, initiated_by=initiated_by, seq=seq,
                         correlation_id=correlation, cflow_id=cflow_id,
                         txn_ref=txn_ref, product=product, uri=uri,
                         content_type=content_type, size_bytes=len(raw))
    if task_type == "txn_request":
        # So Egress can classify the switch's eventual answer as txn_response.
        txn_state.note_inbound_request(txn_ref)

    received_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter_ns()
    try:
        async with httpx.AsyncClient(
            verify=False, timeout=settings.txn_switch_post_timeout_s,
        ) as client:
            resp = await client.post(base + uri, content=raw,
                                     headers={"Content-Type": content_type})
    except httpx.TimeoutException:
        return refused("SWITCH_TIMEOUT",
                       f"switch gave no answer within {settings.txn_switch_post_timeout_s}s")
    except Exception as exc:  # noqa: BLE001
        return refused("SWITCH_UNREACHABLE", str(exc)[:300])

    latency_ms = max(0, (time.perf_counter_ns() - started) // 1_000_000)
    ack_bytes = resp.content or b""
    ack_b64 = base64.b64encode(ack_bytes).decode()
    ack_ct = resp.headers.get("content-type", content_type)

    txn_state.record_hop(message_id=f"{message_id}:ack", direction="outgoing",
                         task_type=task_type, initiated_by=initiated_by, seq=seq + 1,
                         correlation_id=correlation, cflow_id=cflow_id,
                         txn_ref=txn_ref, product=product,
                         acks_message_id=message_id, content_type=ack_ct,
                         size_bytes=len(ack_bytes), latency_ms=latency_ms)

    reply = {
        "kind": "txn_ack",
        "acks_message_id": message_id,
        "correlation_id": correlation,
        "txn": {
            "header": {"content_type": ack_ct},
            "metadata": {
                "product": product,
                "content_encoding": "base64",
                "http_status": resp.status_code,
                "txn_ref": txn_ref,
                "size_bytes": len(ack_bytes),
                "acked_at": datetime.now(timezone.utc).isoformat(),
                # The switch timings the plan says the partner reports —
                # NPCI's only visibility into our internal legs.
                "received_at": received_at,
                "responded_at": datetime.now(timezone.utc).isoformat(),
                "latency_ms": latency_ms,
            },
            "txn_payload": ack_b64,
        },
    }
    txn_state.remember_reply(message_id, reply)
    return reply
