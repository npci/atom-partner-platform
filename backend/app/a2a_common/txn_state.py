"""Shared state for the partner's Txn-over-A2A wrappers (docs/upi_over_a2a_plan.md).

The partner side keeps nothing durable for the relay — this module holds what the
two wrappers need across requests: the active cflow per identity (observed from the
cert conversation), which txn_refs arrived as NPCI-initiated requests (so Egress
can classify the switch's answer as a txn_response), an in-memory dedup store of
sent replies, and the partner's own hop log (JSONL — NPCI never sees these internal
legs, this file is the bank's evidence).

Process-lifetime state by design: a restart mid-run loses dedup/classification
history, which fails loudly (misclassified leg → NPCI Ingress refuses it), never
silently. The hop log is the only persistent piece.

That is also why the Egress listener must run in the SAME process as the A2A mount
that feeds this state (see `app.main`).
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("partner.txn")

_lock = threading.Lock()
_cflow_by_identity: dict[str, str] = {}          # "upi/OLV101" -> cflow_id
_inbound_requests: "OrderedDict[str, str]" = OrderedDict()   # txn_ref -> tc hint ("" ok)
_replies: "OrderedDict[str, dict]" = OrderedDict()           # message_id -> sent reply
_CAP = 4096


def correlation_for(cflow_id: str, txn_ref: str) -> str:
    """Same formula as NPCI's (confirmed default): uuid5(cflow + ':' + ref)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{cflow_id}:{txn_ref}"))


# ── Conversation observation (called from the executor) ─────────────────────


def observe_conversation(task_type: str, cflow_id: Optional[str],
                         identity: Optional[dict] = None) -> None:
    """Track the run window off the cert conversation this partner already has:
    setup opens it (the plan's own trigger), sign-off/abort close it.

    The window is keyed by the IDENTITY the conversation certifies — the
    payload's `identity` block when NPCI sends one — falling back to the
    adapter's own PSP org. Keying on the adapter alone leaks across identities
    when one partner fronts several: the second identity's setup would overwrite
    the first's window AND tag its transactions with the wrong cflow."""
    if isinstance(identity, dict) and identity.get("product") and identity.get("org_id"):
        key = f"{identity['product']}/{identity['org_id']}"
    else:
        from app.adapters import get_switch_adapter
        key = f"upi/{get_switch_adapter().bank_config('').psp_org_id}"
    with _lock:
        if task_type == "cert_setup_notification" and cflow_id:
            _cflow_by_identity[key] = cflow_id
            logger.info("txn_state: run open %s cflow=%s", key, cflow_id)
        elif task_type in ("cert_signoff_notification", "cert_completion_signoff",
                           "cert_run_abort"):
            if _cflow_by_identity.pop(key, None):
                logger.info("txn_state: run closed %s (%s)", key, task_type)


def cflow_for(product: str, org_id: str) -> Optional[str]:
    with _lock:
        return _cflow_by_identity.get(f"{product}/{org_id}")


# ── Request/response classification ─────────────────────────────────────────


def note_inbound_request(txn_ref: str) -> None:
    with _lock:
        _inbound_requests[txn_ref] = ""
        while len(_inbound_requests) > _CAP:
            _inbound_requests.popitem(last=False)


def saw_inbound_request(txn_ref: str) -> bool:
    with _lock:
        return txn_ref in _inbound_requests


# ── Dedup ────────────────────────────────────────────────────────────────────


def stored_reply(message_id: str) -> Optional[dict]:
    with _lock:
        return _replies.get(message_id)


def remember_reply(message_id: str, reply: dict) -> None:
    with _lock:
        _replies[message_id] = reply
        while len(_replies) > _CAP:
            _replies.popitem(last=False)


# ── The partner's own hop log ───────────────────────────────────────────────


def record_hop(**fields: Any) -> dict:
    """Append one hop to the JSONL log. Logging failure never breaks a relay."""
    from app.config import settings

    hop = {"ts": datetime.now(timezone.utc).isoformat(), **fields}
    path = (settings.txn_hop_log_path or "").strip()
    if path:
        try:
            p = Path(path).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(hop, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            logger.exception("txn_state: hop log write failed")
    return hop


# ── Test support ─────────────────────────────────────────────────────────────


def _reset() -> None:
    with _lock:
        _cflow_by_identity.clear()
        _inbound_requests.clear()
        _replies.clear()
