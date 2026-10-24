"""Partner Egress — switch HTTP in, A2A txn_* out (docs/upi_over_a2a_plan.md, M3 3c).

Mirror of NPCI's Egress. The partner's switch is configured to post its NPCI-bound
traffic to `https://<this host>:8111/t/upi/{org}`, so every message the switch
sends — a case it self-fires (ReqMandate) or its answer to an NPCI-initiated
request (RespPay) — lands here and crosses the authenticated A2A channel to NPCI.
The engine's <Ack>, relayed back inside the synchronous A2A reply, becomes the HTTP
response the switch reads. Faults give the switch a bare 502 — never a fabricated
ACK or NACK.

TLS is mandatory for switches that hardcode https:// (the simulator trusts any
cert). The listener is started IN the partner's own process (`app.main` startup) on
`TXN_EGRESS_PORT` — it must share the process because `txn_state` (run window,
dedup, classification) is in-memory and is written by the Ingress executor. Run
standalone (`python -m app.a2a_common.txn_egress`) it refuses everything with
NO_ACTIVE_RUN; that mode exists only to probe the listener.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Response

from app.a2a_common import txn_state
from app.a2a_common.txn_ingress import _UPI_URI_RE

logger = logging.getLogger("partner.txn")

app = FastAPI(title="Partner Txn Egress", docs_url=None, redoc_url=None, openapi_url=None)

AGENT_ID = "partner.txn_egress.v1"
_TXNREF_RE = re.compile(r"/urn:txnid:([0-9A-Za-z.\-_]{1,64})$")


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "agent": AGENT_ID}


@app.post("/t/{product}/{org_id}/{engine_path:path}")
async def relay(product: str, org_id: str, engine_path: str, request: Request) -> Response:
    body = await request.body()
    return await _relay(product, org_id, "/" + engine_path, body,
                        request.headers.get("content-type", "application/xml"))


async def _relay(product: str, org_id: str, uri: str,
                 body: bytes, content_type: str) -> Response:
    """Separated from the route so tests drive it without a TLS listener."""
    from app.adapters import get_switch_adapter

    started = time.perf_counter_ns()
    m = _TXNREF_RE.search(uri)
    txn_ref = m.group(1) if m else ""

    def fault_out(code: str, detail: str, **extra) -> Response:
        txn_state.record_hop(message_id=str(uuid.uuid4()), direction="outgoing",
                             task_type=extra.get("task_type", "txn_request"),
                             initiated_by=extra.get("initiated_by", "BANK"),
                             product=product, uri=uri, txn_ref=txn_ref or None,
                             cflow_id=extra.get("cflow_id"),
                             fault_code=code, fault_detail=detail)
        logger.warning("txn_egress refused %s/%s %s: %s %s",
                       product, org_id, uri, code, detail)
        return Response(status_code=502)

    if product != "upi" or get_switch_adapter().switch_base(product, org_id) is None:
        return fault_out("UNKNOWN_PARTNER",
                         f"this partner fronts no switch for {product}/{org_id}")
    if not _UPI_URI_RE.match(uri):
        return fault_out("UNKNOWN_TARGET", "uri not on the upi allow-list")

    cflow_id = txn_state.cflow_for(product, org_id)
    if not cflow_id:
        # The partner self-fires only after cert_setup_notification opened the
        # window; a fire outside it fails loudly, never silently direct.
        return fault_out("NO_ACTIVE_RUN",
                         "no cert_setup_notification observed for this identity")

    if txn_state.saw_inbound_request(txn_ref):
        task_type, initiated_by, seq = "txn_response", "NPCI", 3
    else:
        task_type, initiated_by, seq = "txn_request", "BANK", 1

    message_id = str(uuid.uuid4())
    correlation = txn_state.correlation_for(cflow_id, txn_ref)
    b64 = base64.b64encode(body).decode()

    from app.config import settings
    envelope = {
        "protocol_version": "1.0",
        "message_id": message_id,
        "task_type": task_type,
        "from": f"bank-{org_id}",
        "agent_id": AGENT_ID,
        "correlation_id": correlation,
        "cflow_id": cflow_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "initiated_by": initiated_by,
            "certification": {"product": product, "org_id": org_id},
            "txn": {
                "header": {"content_type": content_type},
                "metadata": {
                    "product": product,
                    "content_encoding": "base64",
                    "uri": uri,
                    "txn_ref": txn_ref,
                    "size_bytes": len(body),
                    "sent_at": datetime.now(timezone.utc).isoformat(),
                    "timeout_ms": int(settings.txn_a2a_call_timeout_s * 1000),
                },
                "txn_payload": b64,
            },
        },
    }

    txn_state.record_hop(message_id=message_id, direction="outgoing",
                         task_type=task_type, initiated_by=initiated_by, seq=seq,
                         correlation_id=correlation, cflow_id=cflow_id,
                         txn_ref=txn_ref, product=product, uri=uri,
                         content_type=content_type, size_bytes=len(body))

    reply = await _send(cflow_id, message_id, envelope)
    latency_ms = max(0, (time.perf_counter_ns() - started) // 1_000_000)

    txn = (reply or {}).get("txn") if isinstance(reply, dict) else None
    if not isinstance(txn, dict):
        fault = (reply or {}).get("fault") if isinstance(reply, dict) else None
        code = (fault or {}).get("fault_code") or "SWITCH_UNREACHABLE"
        detail = (fault or {}).get("fault_detail") or "no ACK in the A2A reply"
        txn_state.record_hop(message_id=f"{message_id}:fault", direction="incoming",
                             task_type=task_type, initiated_by=initiated_by,
                             seq=seq + 1, correlation_id=correlation,
                             cflow_id=cflow_id, txn_ref=txn_ref, product=product,
                             acks_message_id=message_id, fault_code=code,
                             fault_detail=detail, latency_ms=latency_ms)
        logger.warning("txn_egress fault for %s %s: %s %s", org_id, uri, code, detail)
        return Response(status_code=502)

    try:
        ack_bytes = base64.b64decode(txn.get("txn_payload") or "", validate=True)
    except Exception:  # noqa: BLE001
        return fault_out("SWITCH_UNREACHABLE", "ACK txn_payload not valid base64",
                         task_type=task_type, initiated_by=initiated_by,
                         cflow_id=cflow_id)

    meta = txn.get("metadata") or {}
    ack_ct = (txn.get("header") or {}).get("content_type") or "application/xml"
    txn_state.record_hop(message_id=f"{message_id}:ack", direction="incoming",
                         task_type=task_type, initiated_by=initiated_by, seq=seq + 1,
                         correlation_id=correlation, cflow_id=cflow_id,
                         txn_ref=txn_ref, product=product,
                         acks_message_id=message_id, content_type=ack_ct,
                         size_bytes=meta.get("size_bytes") or len(ack_bytes),
                         latency_ms=latency_ms)
    return Response(content=ack_bytes, media_type=ack_ct,
                    status_code=int(meta.get("http_status") or 200))


def _auth_context(bank_org_id: str | None):
    """(token, NPCI base URL, HMAC secret) from the partner's settings, or None.

    Blocking — `npci_client.authenticate` does a synchronous HTTP handshake and
    reads partner_settings through a DB session — so callers run it in a thread."""
    from app import npci_client
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        token = npci_client.authenticate(db, bank_org_id=bank_org_id)
        if not token:
            return None
        return (token, npci_client._get_a2a_base_url(db),
                npci_client._get_setting(db, "npci_hmac_secret") or None)
    finally:
        db.close()


async def _send(cflow_id: str, message_id: str, envelope: dict) -> dict | None:
    """One authenticated A2A send to NPCI, reply artifact back. Separated so
    tests monkeypatch the wire away."""
    from app.a2a_common.client import send_a2a_message
    from app.adapters import get_switch_adapter
    from app.config import settings

    try:
        ctx = await asyncio.to_thread(_auth_context, get_switch_adapter().bank_org_id())
        if ctx is None:
            raise RuntimeError("NPCI authentication failed")
        token, base_url, hmac_secret = ctx
        return await send_a2a_message(
            base_url=base_url,
            context_id=cflow_id,
            task_id=message_id,
            data=envelope,
            auth_header=f"Bearer {token}",
            hmac_secret=hmac_secret,
            timeout=settings.txn_a2a_call_timeout_s,
        )
    except Exception as exc:  # noqa: BLE001 — every transport shape is one fault
        logger.warning("txn_egress send failed: %s", str(exc)[:200])
        return None


if __name__ == "__main__":
    import uvicorn

    from app.config import settings

    uvicorn.run(app, host="0.0.0.0", port=settings.txn_egress_port,
                ssl_keyfile=settings.txn_egress_ssl_key,
                ssl_certfile=settings.txn_egress_ssl_cert)
