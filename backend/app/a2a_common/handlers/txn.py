"""Txn over A2A — declared task types, refuse-loudly fallback.

The real Ingress is `app.a2a_common.txn_ingress`, intercepted by the executor
BEFORE handler dispatch (it needs the full envelope for `message_id` dedup, which
handlers never see). These registry entries keep the declared contract set
identical across the repos and are the fallback if that interception is ever
bypassed: the generic accepted-ack fall-through would be a fabricated ACK, which a
wrapper must never produce.
"""
from __future__ import annotations


def _txn_not_enabled(body, db) -> dict:
    return {
        "kind": "txn_ack",
        "fault": {
            "fault_code": "NOT_ENABLED",
            "fault_detail": "txn_* must be processed by the executor's txn_ingress "
                            "interception — reaching this handler is a wiring bug",
        },
    }


def handle_txn_request(body, db) -> dict:
    return _txn_not_enabled(body, db)


def handle_txn_response(body, db) -> dict:
    return _txn_not_enabled(body, db)
