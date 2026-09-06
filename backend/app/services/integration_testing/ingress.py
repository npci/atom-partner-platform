# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Ingress: a local HTTP request → an A2A exchange with the authority (ITA I-4).

The REVERSE direction's near end. The External API (or anything on the bank's
side that must call back) points at this platform, and the request is carried
verbatim to a target the AUTHORITY resolves against ITS OWN allowlist —
typically the Simulator's callback API. Mirror of the NPCI repo's
`services/integration_testing/ingress.py` (the forward direction's near end);
the differences are exactly the transport: this side has ONE authority peer
(no partner parameter), sends through `npci_client.send_task_async` with the
§6 budget threaded, and reads the reply from the ITA-3 return value — the
ITA-2 receipt with the §5.2 payload merged in.

What this module deliberately does NOT do (same three as the mirror):

* **It does not resolve the target.** The alias travels; the far side resolves
  it. Resolving here and sending a URL would make this platform the SSRF
  vector for the other one (ITA §2).
* **It does not touch the query string.** It hands the raw string to the
  contract encoder, which carries it opaquely (ITA §12.5).
* **It does not retry.** A tunnelled POST is a business call on the far side;
  replaying it would duplicate whatever it did.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Mapping, Sequence

from sqlalchemy.orm import Session

from app.a2a_common.integration_contract import (
    ErrorCode,
    HttpRequestSpec,
    HttpResponseSpec,
    TunnelError,
    classify_headers,
    decode_response,
    encode_request,
)
from app.config import settings

logger = logging.getLogger(__name__)

__all__ = ["TunnelResult", "forward_exchange"]


class TunnelResult:
    """What the ingress route returns: either a response or a tunnel error."""

    def __init__(self, *, exchange_id: str, response: HttpResponseSpec | None = None,
                 error: Mapping[str, Any] | None = None, elapsed_ms: int | None = None):
        self.exchange_id = exchange_id
        self.response = response
        self.error = dict(error) if error else None
        self.elapsed_ms = elapsed_ms

    @property
    def failed(self) -> bool:
        return self.error is not None


def _error(exchange_id: str, code: str, detail: str) -> TunnelResult:
    logger.warning("tunnel exchange=%s failed code=%s detail=%s", exchange_id, code, detail)
    return TunnelResult(exchange_id=exchange_id, error={"code": code, "detail": detail})


async def forward_exchange(
    *,
    db: Session,
    alias: str,
    method: str,
    path: str,
    query: str,
    headers: Sequence[Sequence[str]],
    body: bytes,
    cert_context: Mapping[str, Any] | None = None,
    exchange_id: str | None = None,
    change_id: str | None = None,
) -> TunnelResult:
    """Carry one HTTP exchange to the authority and bring the response home.

    Returns a `TunnelResult` rather than raising: every failure the far side
    can name is a structured code the caller can assert on, and an exception
    here would collapse them all into a 500.
    """
    exchange_id = exchange_id or str(uuid.uuid4())
    dropped_names: list[str] = []

    def _finish(result: TunnelResult) -> TunnelResult:
        # I-9: one row per hop, best effort — a failed exchange must be
        # diagnosable from the row alone, without logs.
        from app.services.integration_testing.observability import record_exchange

        record_exchange(
            db, direction="ingress", exchange_id=result.exchange_id,
            alias=alias, method=method, path=path,
            # NET-F21: `query` was a parameter of this function all along and
            # was never passed on. Recorded verbatim, so a hop that selected a
            # contract via `?pack=` is distinguishable from one that did not.
            query=query,
            status=result.response.status if result.response else None,
            error_code=(result.error or {}).get("code"),
            request_bytes=len(body or b""),
            response_bytes=len(result.response.body or b"") if result.response else 0,
            elapsed_ms=result.elapsed_ms,
            dropped_headers=dropped_names or None,
            cert_context=cert_context,
        )
        return result

    if not settings.integration_testing_enabled:
        return _finish(_error(exchange_id, ErrorCode.TUNNEL_DISABLED,
                              "integration_testing_enabled is false on this platform"))

    # Hop-by-hop and recomputed headers are stripped HERE, before the wire, so
    # the far side never has to guess which of them described our connection.
    forwarded, dropped = classify_headers(headers)
    dropped_names[:] = [n for n, _ in dropped]
    if dropped:
        logger.info("tunnel exchange=%s dropped %d hop-by-hop/recomputed header(s): %s",
                    exchange_id, len(dropped), dropped_names)

    budget_s = float(settings.integration_testing_a2a_timeout_s)
    try:
        payload = encode_request(
            exchange_id=exchange_id,
            alias=alias,
            request=HttpRequestSpec(
                method=method, path=path, query=query,
                headers=tuple((str(n), str(v)) for n, v in forwarded),
                body=body or b"",
            ),
            # The far side subtracts its own elapsed time from this and refuses
            # a call it cannot finish inside the remainder.
            deadline_ms=int(float(settings.integration_testing_target_timeout_s) * 1000),
            hop=1,
            cert_context=cert_context,
            max_body_bytes=settings.integration_testing_max_body_bytes,
        )
    except TunnelError as exc:
        return _finish(_error(exchange_id, exc.code, exc.detail))

    started = time.perf_counter()
    # Imported here so the module stays importable without the A2A SDK, which
    # the pure contract tests do not install.
    from app.npci_client import send_task_async

    reply = await send_task_async(
        db, "http_exchange_request", change_id, payload,
        correlation_id=exchange_id,
        # The middle layer of the §6 budget. Without it the transport's own
        # 30s default fires below the 60s target ceiling and every slow case
        # fails as a transport error rather than a target timeout.
        timeout=budget_s,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    if reply is None:
        return _finish(_error(exchange_id, ErrorCode.TARGET_UNREACHABLE,
                              "A2A send failed (no reply; tunnel exchanges are "
                              "excluded from the retry sweeper by design)"))

    # The reply is the executor's receipt with the handler dict merged (ITA-2).
    # Tolerate the pre-merge shape too, where the exchange rode under "message".
    body_out: Any = reply
    if isinstance(body_out, Mapping) and "exchange_id" not in body_out:
        inner = body_out.get("message")
        if isinstance(inner, Mapping):
            body_out = inner
        else:
            return _finish(_error(
                exchange_id, ErrorCode.TARGET_UNREACHABLE,
                "authority reply carried no exchange payload "
                f"(status={body_out.get('status')!r})"))

    try:
        decoded = decode_response(
            body_out, max_body_bytes=settings.integration_testing_max_body_bytes)
    except TunnelError as exc:
        return _finish(_error(exchange_id, exc.code, exc.detail))

    if decoded.failed:
        logger.info("tunnel exchange=%s far side returned %s", exchange_id,
                    decoded.error.get("code"))
        return _finish(TunnelResult(exchange_id=exchange_id, error=decoded.error,
                                    elapsed_ms=elapsed_ms))
    return _finish(TunnelResult(exchange_id=exchange_id, response=decoded.response,
                                elapsed_ms=decoded.elapsed_ms or elapsed_ms))
