# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Exchange telemetry (ITA I-9) — one row per tunnelled hop, best effort.

RECORDING MUST NEVER BREAK AN EXCHANGE: every function here swallows and logs
its own failures, because a telemetry INSERT that aborts a live tunnelled
call inverts the point of having telemetry. The write is committed on its
own; a failure rolls back only itself.

Mirror of the NPCI repo's module (only the model import differs — this
platform's models live in one file). Keep the bodies in step.
"""
from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from app.models import IntegrationExchange

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

__all__ = ["record_exchange", "record_from_wire"]


def record_exchange(
    db: "Session",
    *,
    direction: str,
    exchange_id: str,
    alias: str,
    method: str,
    path: str,
    # NET-F21. Stored VERBATIM and never normalised — the wire already carries
    # it that way (integration_contract emits `"query": request.query or ""`),
    # and re-encoding it here would defeat the point of recording it: contract
    # selection rides on `?pack=`, so the row has to be able to prove the exact
    # bytes that were sent. Default None, not "", so a caller that does not
    # supply it is distinguishable from a hop that carried no query.
    query: str | None = None,
    status: int | None = None,
    error_code: str | None = None,
    request_bytes: int = 0,
    response_bytes: int = 0,
    elapsed_ms: int | None = None,
    dropped_headers: Sequence[str] | None = None,
    correlation_id: str | None = None,
    cert_context: Mapping[str, Any] | None = None,
) -> None:
    """Persist one hop. Best effort — never raises."""
    try:
        db.add(IntegrationExchange(
            exchange_id=exchange_id, direction=direction, alias=alias,
            method=(method or "?")[:10], path=(path or "")[:1000],
            query=(query[:1000] if query is not None else None),
            status=status, error_code=error_code,
            request_bytes=int(request_bytes or 0),
            response_bytes=int(response_bytes or 0),
            elapsed_ms=elapsed_ms,
            dropped_headers=list(dropped_headers) if dropped_headers else None,
            correlation_id=correlation_id or exchange_id,
            cert_context=dict(cert_context) if cert_context else None,
        ))
        db.commit()
    except Exception:  # noqa: BLE001 — telemetry must never break the exchange
        logger.exception("integration_exchanges write failed for exchange=%s",
                         exchange_id)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def _b64_len(value: object) -> int:
    if not isinstance(value, str) or not value:
        return 0
    try:
        return len(base64.b64decode(value))
    except Exception:  # noqa: BLE001 — telemetry, not validation
        return 0


def record_from_wire(db: "Session", *, direction: str,
                     request_payload: Mapping[str, Any] | None,
                     result: Mapping[str, Any] | None,
                     correlation_id: str | None = None) -> None:
    """Persist one hop from the §5.1 request payload + §5.2 result dicts —
    the shapes an egress call site has in hand. Best effort, like everything
    here: a malformed payload yields a sparse row, not an exception."""
    request_payload = request_payload if isinstance(request_payload, Mapping) else {}
    result = result if isinstance(result, Mapping) else {}
    request = request_payload.get("request") or {}
    response = result.get("response") or {}
    error = result.get("error") or {}
    record_exchange(
        db,
        direction=direction,
        exchange_id=str(result.get("exchange_id")
                        or request_payload.get("exchange_id") or "unknown"),
        alias=str((request_payload.get("target") or {}).get("alias") or "?"),
        method=str(request.get("method") or "?"),
        path=str(request.get("path") or ""),
        # NET-F21: the wire has carried this all along (integration_contract
        # :264 emits it, :346 reads it back) — this call site simply took
        # method and path from the same dict and dropped it on the floor.
        query=str(request.get("query") or "") if "query" in request else None,
        status=response.get("status") if isinstance(response.get("status"), int) else None,
        error_code=str(error.get("code")) if error.get("code") else None,
        request_bytes=_b64_len(request.get("body_b64")),
        response_bytes=_b64_len(response.get("body_b64")),
        elapsed_ms=result.get("elapsed_ms")
        if isinstance(result.get("elapsed_ms"), int) else None,
        correlation_id=correlation_id,
        cert_context=request_payload.get("cert_context") or None,
    )
