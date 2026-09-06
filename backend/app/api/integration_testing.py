# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The integration-testing tunnel ingress, partner side — an H3 interface (ITA I-4).

The REVERSE direction's front door: the External API (or anything bank-side
that must call back toward the authority's network) points a real HTTP client
here, and everything it sends is carried to a target the AUTHORITY resolves
against its own allowlist. Mirror of the NPCI repo's
`api/integration_testing.py`, minus the partner path segment — this platform
has exactly one authority peer, so the route is `/{alias}/{path}`.

H3 = externally reachable and hostile (security skill §4): off by default,
size-capped in the agent (not only at any gateway), aggressive timeouts,
strict rejection. **No per-caller authorization in v1 for the same recorded
reason as the mirror**: the tunnel is confirmed dev-only, and the control that
matters is on the receiving side — the authority resolves the alias against
its own allowlist and refuses anything else. If this ever ships beyond dev,
that assumption breaks and this route needs authentication before anything
else.
"""
from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.api.auth import require_admin
from app.config import settings
from app.database import get_db
from app.models import PartnerUser
from app.services.integration_testing.ingress import forward_exchange

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integration-testing", tags=["integration-testing"])

# Dashboard-facing reads live under /api like every other admin router, so the
# frontend's `API_BASE + path` convention reaches them unchanged (the root
# `/integration-testing` mount is the RIG's surface — tunnel + outcome channel
# — and the dev proxy/edge rewrite only exposes `/a2a-partner/api/*` to the
# browser; a root-mounted read is unreachable from the UI without a new edge
# rule).
admin_router = APIRouter(prefix="/api/integration-testing",
                         tags=["integration-testing"])

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

# Mapped so a tunnel failure reaches the caller as a plausible HTTP status
# rather than a blanket 500 — the caller asserts on `X-Tunnel-Error` for the
# precise code.
_STATUS_FOR = {
    "tunnel_disabled": 503,
    "unknown_alias": 404,
    "path_not_allowed": 403,
    "payload_too_large": 413,
    "target_timeout": 504,
    "target_unreachable": 502,
    "hop_limit_exceeded": 508,
    "digest_mismatch": 502,
    "malformed_exchange": 400,
    # ITA-5: the far egress protecting its target — transient, retry later.
    "bulkhead_saturated": 503,
    "circuit_open": 503,
}


def _rig_authorised(request: Request, db: Session) -> Response | None:
    """Authenticate the certification rig against `cert_trigger_secret`.

    Returns None when authorised, or the Response to send back otherwise.

    WHY THIS ROUTE IS AUTHENTICATED WHEN THE TUNNEL ROUTE BELOW IS NOT
    (PTNR-F37). The module docstring's exemption rests on one clause: "the
    control that matters is on the receiving side — the authority resolves the
    alias against its own allowlist and refuses anything else." That is true of
    `/{alias}/{path}`, where the authority independently constrains every
    exchange. It is NOT true here. A `reporter="bank"` result is taken by the
    authority verbatim — `authority_handlers.py` maps the reported status
    string straight onto the run's result row and runs no assertions on that
    path — so there is no receiving-side control to lean on. Without a check
    here, any caller who can reach this port makes THIS platform, holding its
    own valid HMAC+JWT credentials, assert a pass the operator never
    authorised, and `cert_join` certifies on `failed == 0`.

    The secret is the one the rig already holds: `fire_trigger` sends it as a
    bearer token on every trigger, so the rig can present it back with no new
    credential and no new plumbing.

    FAIL CLOSED when unset, matching this platform's house rule for a missing
    inbound secret (`PartnerAuthMiddleware` 503s rather than admitting) — an
    unconfigured verdict channel must not be an open one.
    """
    from app.models import PartnerSetting

    row = db.get(PartnerSetting, "cert_trigger_secret")
    expected = ((row.value if row else "") or "").strip()
    if not expected:
        return Response(
            status_code=503,
            content=b'{"error":"cert_trigger_secret is not configured; the '
                    b'certification result channel is closed until it is"}',
            media_type="application/json",
        )
    presented = (request.headers.get("authorization") or "").strip()
    if not hmac.compare_digest(presented, f"Bearer {expected}"):
        logger.warning(
            "SECURITY_EVENT event=cert_case_outcome_unauthorised severity=high "
            "presented=%s — a cert case outcome was rejected for a bad or "
            "absent bearer token", "yes" if presented else "no",
        )
        return Response(
            status_code=401,
            content=b'{"error":"bad or missing certification rig credential"}',
            media_type="application/json",
        )
    return None


@router.post("/cert-case-outcome", status_code=202)
def report_cert_case_outcome(body: dict, request: Request,
                             db: Session = Depends(get_db)) -> dict:
    """The certification rig reports one executed case's outcome; this
    platform forwards it to the authority as `cert_case_result`
    (reporter=bank) — the report that replaces the authority's not_reported
    placeholder (ITA I-6/I-7).

    AUTHENTICATED against `cert_trigger_secret` — see `_rig_authorised` for
    why this route cannot take the tunnel route's dev-only exemption.
    """
    if not settings.integration_testing_enabled:
        return {"forwarded": False, "reason": "tunnel disabled"}

    denied = _rig_authorised(request, db)
    if denied is not None:
        return denied

    npci_change_id = str(body.get("npci_change_id") or "").strip()
    case_id = str(body.get("case_id") or "").strip()
    status = str(body.get("status") or "").strip().lower()
    attempt = int(body.get("cert_attempt") or 1)
    details = body.get("details") if isinstance(body.get("details"), dict) else {}
    if not npci_change_id or not case_id or status not in ("passed", "failed", "error"):
        return Response(status_code=422,
                        content=b'{"error":"npci_change_id, case_id and '
                                b'status in {passed|failed|error} are required"}',
                        media_type="application/json")

    # PERSIST FIRST, forward second: the execution record is this platform's
    # own evidence of what its application answered and how it was graded —
    # it must survive even if the A2A send fails (the authority's deadline
    # sweep covers the wire; nothing covers a report this side never kept).
    from app.models import CertCaseExecution

    db.add(CertCaseExecution(
        npci_change_id=npci_change_id, case_id=case_id, cert_attempt=attempt,
        cflow_id=(details.get("cert_context") or {}).get("cflow_id"),
        status=status, details=details,
    ))
    db.commit()

    from app.npci_client import send_cert_case_result

    reply = send_cert_case_result(db, npci_change_id, case_id, status,
                                  attempt=attempt, details=details,
                                  reporter="bank")
    logger.info("cert-case-outcome: cflow=%s attempt=%d case=%s status=%s "
                "forwarded=%s",
                (details.get("cert_context") or {}).get("cflow_id"),
                attempt, case_id, status, reply is not None)
    return {"forwarded": reply is not None, "case_id": case_id,
            "status": status}


@admin_router.get("/cert-executions")
def list_cert_executions(npci_change_id: str, limit: int = 200,
                         user: PartnerUser = Depends(require_admin),
                         db: Session = Depends(get_db)) -> dict:
    """The partner's own per-case certification evidence, newest attempt
    first: what the application was asked, what it answered (raw payloads,
    capped), and how the round's contract graded it. Admin-authed like
    `/exchanges` — payloads are business data, not tunnel plumbing."""
    from app.models import CertCaseExecution

    rows = (db.query(CertCaseExecution)
            .filter(CertCaseExecution.npci_change_id == npci_change_id)
            .order_by(CertCaseExecution.cert_attempt.desc(),
                      CertCaseExecution.case_id.asc(),
                      CertCaseExecution.created_at.desc())
            .limit(max(1, min(int(limit), 500))).all())
    return {"executions": [
        {
            "id": r.id, "case_id": r.case_id, "cert_attempt": r.cert_attempt,
            "cflow_id": r.cflow_id, "status": r.status, "details": r.details,
            "at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]}


@router.get("/exchanges")
def list_exchanges(limit: int = 50,
                   user: PartnerUser = Depends(require_admin),
                   db: Session = Depends(get_db)) -> dict:
    """The I-9 admin view: recent tunnelled exchanges, newest first — each row
    diagnosable without logs. Registered BEFORE the catch-all so the literal
    path wins; admin-authed, unlike the tunnel itself (dev-only, deliberately
    unauthenticated — see the module docstring)."""
    from app.models import IntegrationExchange

    rows = (db.query(IntegrationExchange)
            .order_by(IntegrationExchange.created_at.desc())
            .limit(max(1, min(int(limit), 200))).all())
    return {"exchanges": [
        {
            "exchange_id": r.exchange_id, "direction": r.direction,
            "alias": r.alias, "method": r.method, "path": r.path,
            "status": r.status, "error_code": r.error_code,
            "request_bytes": r.request_bytes, "response_bytes": r.response_bytes,
            "elapsed_ms": r.elapsed_ms, "dropped_headers": r.dropped_headers,
            "correlation_id": r.correlation_id, "cert_context": r.cert_context,
            "at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]}


@router.api_route("/{alias}/{target_path:path}", methods=_METHODS)
async def tunnel_exchange(
    alias: str,
    target_path: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Carry one HTTP exchange to the authority, addressed to `alias`.

    `alias` is a NAME, not a URL, and this side never resolves it — the far
    platform does, against its own allowlist (ITA §2).
    """
    if not settings.integration_testing_enabled:
        return Response(status_code=503, content=b"integration testing tunnel is disabled",
                        headers={"X-Tunnel-Error": "tunnel_disabled"})

    body = await request.body()
    if len(body) > settings.integration_testing_max_body_bytes:
        # Enforced HERE and not only at a gateway: §16 — no gateway-only security.
        return Response(status_code=413, content=b"request body too large",
                        headers={"X-Tunnel-Error": "payload_too_large"})

    result = await forward_exchange(
        db=db,
        alias=alias,
        method=request.method,
        # Rebuilt with the leading slash the target expects; the path segment
        # arrives without one.
        path="/" + (target_path or ""),
        # VERBATIM. `request.url.query` is the raw string Starlette parsed off
        # the request line — not a re-encoding of parsed parameters. Contract
        # selection rides on `?pack=`, so normalising here would present as
        # "certified against baseline" (ITA §12.5).
        query=request.url.query or "",
        # `.raw` preserves repeats and original casing; `.items()` would not.
        headers=[(k.decode("latin-1"), v.decode("latin-1"))
                 for k, v in request.headers.raw],
        body=body,
    )

    if result.failed:
        code = str(result.error.get("code") or "target_unreachable")
        detail = str(result.error.get("detail") or "")
        return Response(
            status_code=_STATUS_FOR.get(code, 502),
            content=detail.encode("utf-8"),
            headers={"X-Tunnel-Error": code, "X-Tunnel-Exchange": result.exchange_id},
        )

    response = result.response
    # Hop-by-hop and length headers are dropped on the way back for the same
    # reason as on the way out: they described the far connection. Starlette
    # recomputes Content-Length for the body we return.
    from app.a2a_common.integration_contract import classify_headers

    forwarded, _dropped = classify_headers(response.headers)
    out = Response(status_code=response.status, content=response.body)
    for name, value in forwarded:
        # append, not assign: repeats such as Set-Cookie must survive.
        out.headers.append(name, value)
    out.headers["X-Tunnel-Exchange"] = result.exchange_id
    return out
