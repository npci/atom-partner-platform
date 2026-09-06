# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Partner Platform — FastAPI application."""
import logging
import os
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.auth import seed_admin
from app.api.dashboard import router as dashboard_router
from app.api.feasibility import router as feasibility_router
from app.api.users import router as users_router
from app.config import settings
from app.database import SessionLocal, init_db

# Catch sites that must not interpolate `str(exc)` into a normal log line
# (CWE-209 — third-party messages can carry connection strings, hosts and
# server paths) log the exception TYPE at WARNING/ERROR and pair it with a
# `logger.debug(..., exc_info=True)` companion holding the full message and
# traceback. That detail is therefore ON by default outside production and
# available in production by setting PARTNER_LOG_LEVEL=DEBUG deliberately,
# rather than leaking into every environment by accident.
_default_log_level = "INFO" if settings.app_env == "production" else "DEBUG"
_log_level = os.getenv("PARTNER_LOG_LEVEL", _default_log_level).upper()
logging.basicConfig(
    level=getattr(logging, _log_level, logging.INFO),
    format="%(asctime)s  %(levelname)-5s  [%(name)s]  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# Third-party DEBUG is far noisier than ours and can itself log request
# bodies and auth headers — keep those at INFO regardless.
for _noisy in ("httpx", "httpcore", "urllib3", "sqlalchemy.engine", "openai", "anthropic"):
    logging.getLogger(_noisy).setLevel(logging.INFO)
logger = logging.getLogger("partner")

_is_prod = settings.app_env == "production"
app = FastAPI(
    title=f"{settings.partner_name} — Partner Platform",
    version="1.0.0",
    docs_url=None if _is_prod else "/docs",
    redoc_url=None if _is_prod else "/redoc",
    openapi_url=None if _is_prod else "/openapi.json",
)

# Slice 3 of A2A security hardening — replace `allow_origins=["*"]`
# (which is incompatible with `allow_credentials=True` per the CORS
# spec anyway) with the configured NPCI platform origin plus a small
# fixed dev-host allowlist. Override via `PARTNER_CORS_EXTRA_ORIGINS`
# env (comma-separated) when fronting the partner UI from a non-default
# origin during integration testing.
_cors_origins = [settings.npci_platform_url.rstrip("/")]
_extra = os.getenv("PARTNER_CORS_EXTRA_ORIGINS", "").strip()
if _extra:
    _cors_origins.extend(o.strip().rstrip("/") for o in _extra.split(",") if o.strip())
# Local dev origins for the partner-side React app
_cors_origins.extend(["http://localhost:3000", "http://localhost:5173"])

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── HSTS middleware (SAST finding F-005) ─────────────────────────────────────
# Sets Strict-Transport-Security on every response so browsers always use HTTPS
# for this domain. The max-age is 1 year with includeSubDomains for production;
# development uses a shorter duration so local cert rotation doesn't lock devs out.
_HSTS_MAX_AGE = 31536000 if _is_prod else 86400  # 1 year prod, 1 day dev


class HSTSMiddleware:
    """ASGI middleware that adds the Strict-Transport-Security header."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_hsts(message):
            if message["type"] == "http.response.start":
                headers = message.get("headers", [])
                # Don't duplicate if already set
                if not any(k.lower() == b"strict-transport-security" for k, _ in headers):
                    headers = list(headers)
                    headers.append(
                        (b"strict-transport-security",
                         f"max-age={_HSTS_MAX_AGE}; includeSubDomains".encode())
                    )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_hsts)


app.add_middleware(HSTSMiddleware)

# ── Global inbound body-size backstop (Finding 9) ────────────────────────────
# Defense-in-depth for every route OUTSIDE the A2A mount (which has its own,
# stricter, streaming-aware limit via PartnerHmacMiddleware). See
# docs/adr/ADR-0004-hostility-tier-registry.md for where the limit is sourced.
from app.core.body_size_middleware import MaxBodySizeMiddleware

app.add_middleware(MaxBodySizeMiddleware, max_bytes=settings.a2a_max_request_body_bytes)

app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(feasibility_router)
app.include_router(users_router)

# ITA-4 — the reverse tunnel's ingress (H3; hard-disabled unless
# integration_testing_enabled, which production config refuses). Registered
# unconditionally so a config flip does not change the route table shape.
from app.api.integration_testing import (
    admin_router as integration_testing_admin_router,
    router as integration_testing_router,
)

app.include_router(integration_testing_router)
# The dashboard-facing half, under /api so the browser can reach it: the edge
# only rewrites `/a2a-partner/api/*` toward this service, so a root-mounted
# read renders as a 404 in the UI with a perfectly healthy backend behind it.
app.include_router(integration_testing_admin_router)


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=500, content={"detail": "An internal error occurred"})


# ── A2A SDK mount ────────────────────────────────────────────────────────────
# Native JSON-RPC endpoint at /a2a-rpc/rpc + agent card at the SDK's standard
# /.well-known/agent-card.json. (The legacy POST /api/a2a/tasks/send router was
# decommissioned; the SDK JSON-RPC mount is the sole inbound A2A path.)
#
# Lazy-imported so a missing `a2a-sdk` install does not block partner
# startup — the SDK path is unused until NPCI flips this partner over.
try:
    from app.a2a_common import build_a2a_components
    from app.a2a_common.auth_middleware import PartnerAuthMiddleware
    from app.a2a_common.hmac_middleware import PartnerHmacMiddleware
    from app.a2a_common.partner_card import PARTNER_AGENT_CARD
    from app.a2a_common.partner_executor import PartnerAgentExecutor
    from app.a2a_common.rate_limit_middleware import A2ARateLimitMiddleware
except Exception as _a2a_import_err:  # noqa: BLE001
    logger.warning("a2a-sdk not importable; skipping /a2a-rpc mount", exc_info=True)
else:
    try:
        _a2a_sub_app, _a2a_card_routes = build_a2a_components(
            agent_card=PARTNER_AGENT_CARD,
            executor=PartnerAgentExecutor(),
            # In-memory store — Slice 5 swaps in get_task_store() so
            # Tasks survive worker restarts.
            task_store=None,
            rpc_url="/rpc",
            # Finding 10 (security_architecture_skills.md §4.2/§11.3):
            # application-layer rate limit on the A2A ingress, independent
            # of whether an operator has deployed nginx in front of the
            # stack. See docs/adr/ADR-0004-hostility-tier-registry.md.
            rate_limit_middleware=A2ARateLimitMiddleware,
            rate_limit_options={"limit_rps": settings.a2a_rate_limit_rps},
            # Slice 3 of A2A security hardening: validate inbound Bearer
            # JWTs signed by NPCI. FAIL-CLOSED (docs/adr/ADR-0003) when
            # `partner_settings.npci_jwt_secret` isn't configured.
            auth_middleware=PartnerAuthMiddleware,
            # Slice 5 of A2A security hardening: verify the X-NPCI-Signature
            # envelope before JWT decode. FAIL-CLOSED (docs/adr/ADR-0003)
            # when `partner_settings.npci_hmac_secret` isn't installed.
            hmac_middleware=PartnerHmacMiddleware,
        )
        app.mount("/a2a-rpc", _a2a_sub_app)
        for _r in _a2a_card_routes:
            app.add_route(_r.path, _r.endpoint, methods=list(_r.methods))
        logger.info("A2A SDK mount active: /a2a-rpc/rpc")
    except Exception as _a2a_mount_err:  # noqa: BLE001
        logger.exception("A2A SDK mount failed")


@app.on_event("startup")
def on_startup():
    # Fail fast on unsafe/missing hostility-tier configuration BEFORE the DB
    # is touched or any traffic is accepted — security_architecture_skills.md
    # §4.3 Startup Validation Rule. See docs/core/hostility.py.
    from app.core.hostility import validate_at_startup
    validate_at_startup()
    # Refuse a multi-worker boot: A2A rate limiting and the revision-context
    # cache are per-process, so extra workers silently multiply the effective
    # rate limit (EA_Skills.md P2). See core/runtime.py for the override and
    # the Redis upgrade path.
    from app.core.runtime import validate_single_instance
    validate_single_instance()
    init_db()
    # Seed default admin user if none exists
    db = SessionLocal()
    try:
        seed_admin(db)
    finally:
        db.close()

    # Data retention sweep (Finding 7: security_architecture_skills.md §10.3,
    # EA_Skills.md P6) — daily background purge of superseded generated-code
    # iterations and stale agent-run payloads. See services/retention.py.
    from app.services import retention_scheduler
    retention_scheduler.start()

    # Outbound A2A retry sweep (Finding 12: security_architecture_skills.md
    # §5.4/§11.3, EA_Skills.md P7 "DLQ and replay process") — drains queued
    # OutboundA2ARetry rows on a short interval. See services/outbound_retry.py.
    from app.services import outbound_retry_scheduler
    outbound_retry_scheduler.start()

    logger.info("Partner Platform started: %s", settings.partner_name)


@app.on_event("shutdown")
def on_shutdown():
    """Graceful drain (EA_Skills.md P3 — "safe scale-in with drain/linger
    behavior"). Order matters: stop admitting new jobs and let in-flight ones
    finish FIRST, then stop the background sweeps. Stopping the sweeps first
    would leave a draining job's outbound sends unretried."""
    from app.config import settings
    from app.core.runtime import drain
    from app.services import outbound_retry_scheduler, retention_scheduler

    remaining, elapsed = drain(settings.shutdown_drain_timeout_s)
    if remaining:
        # The window expired with work still running. Mark those rows now,
        # while we still know which they are, rather than leaving them
        # "running" for `_sweep_interrupted_agent_jobs()` to tombstone on the
        # next boot — that sweep is a blunt "everything still running must be
        # dead" and cannot distinguish these from a hard crash.
        _mark_undrained_jobs_interrupted()
        logger.warning(
            "shutdown drained for %.1fs; %d agent job(s) did not finish and were "
            "marked interrupted", elapsed, remaining,
        )

    retention_scheduler.stop()
    outbound_retry_scheduler.stop()


def _mark_undrained_jobs_interrupted() -> None:
    """Close out jobs still in-flight when the drain window expired.

    Best-effort: a failure here must not prevent shutdown from completing —
    `database._sweep_interrupted_agent_jobs()` is the backstop on next boot."""
    from app.core.runtime import inflight_job_ids

    job_ids = inflight_job_ids()
    if not job_ids:
        return
    db = SessionLocal()
    try:
        from app.models import AgentJob
        for job_id in job_ids:
            job = db.get(AgentJob, job_id)
            if job is not None and job.status == "running":
                job.status = "error"
                job.error = "interrupted by a server shutdown — run again"
                job.error_category = "capacity"
                job.error_code = "shutdown_interrupted"
                job.progress = None
                job.finished_at = datetime.now(timezone.utc)
        db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("could not mark undrained agent jobs as interrupted")
    finally:
        db.close()


@app.get("/api/health")
def health():
    """Liveness. Stays 200 while draining — the process IS alive, and a
    liveness probe failing mid-drain would get the container killed before it
    finishes, defeating the drain. Readiness is `/api/ready`."""
    return {"status": "ok", "partner": settings.partner_name}


@app.get("/api/ready")
def ready():
    """Readiness — distinct from liveness (EA_Skills.md P3 "safe scale-in with
    drain/linger behavior").

    Returns 503 once the drain has begun so the load balancer / orchestrator
    stops routing new traffic here while in-flight jobs finish. Without this
    split, a rolling deploy keeps sending work to an instance that is actively
    trying to shut down.
    """
    from fastapi.responses import JSONResponse

    from app.core.runtime import inflight_count, is_accepting

    accepting = is_accepting()
    body = {
        "status": "ready" if accepting else "draining",
        "accepting_jobs": accepting,
        "inflight_jobs": inflight_count(),
    }
    return JSONResponse(body, status_code=200 if accepting else 503)


@app.get("/api/metrics")
def metrics():
    """Application-level metrics for autoscaling decisions (EA_Skills.md P3:
    "Flag when: scaling rules depend only on CPU/memory without application
    metrics" — it asks for queue depth, worker utilization, and saturation
    indicators).

    Deliberately a plain JSON endpoint rather than a Prometheus exposition
    format: the platform has no metrics stack bundled, and a partner's scraper
    can trivially adapt JSON, whereas shipping a Prometheus client would add a
    dependency for a reference deployment that may not use it. The shape is
    the contract; the encoding is not.

    Unauthenticated by design (same as /api/health) so an orchestrator can
    scrape it without credentials — it exposes only counters and saturation
    ratios, never change content, secrets, or partner data.
    """
    from app.agents.revision_context import context_cache_stats
    from app.core.hostility import BOUNDARIES
    from app.core.resilience import _breakers, _bulkheads
    from app.core.runtime import inflight_count, is_accepting

    breakers = {name: cb.state for name, cb in _breakers.items()}

    # Saturation per bulkhead: in_use / max_concurrent. BoundedSemaphore
    # exposes its remaining permits as `_value`; read defensively so a CPython
    # internals change degrades this endpoint to "unknown" rather than 500-ing
    # a scrape that an autoscaler depends on.
    bulkheads = {}
    for name, bh in _bulkheads.items():
        try:
            available = bh._sem._value  # noqa: SLF001 — no public accessor exists
            in_use = max(0, bh.max_concurrent - available)
            bulkheads[name] = {
                "in_use": in_use,
                "max_concurrent": bh.max_concurrent,
                "saturation": round(in_use / bh.max_concurrent, 3) if bh.max_concurrent else None,
            }
        except Exception:  # noqa: BLE001
            bulkheads[name] = {"in_use": None, "max_concurrent": bh.max_concurrent}

    outbound_backlog = None
    db = SessionLocal()
    try:
        from sqlalchemy import func, select

        from app.models import OutboundA2ARetry
        outbound_backlog = db.execute(
            select(func.count(OutboundA2ARetry.id)).where(OutboundA2ARetry.status == "pending")
        ).scalar_one()
    except Exception:  # noqa: BLE001 — a metrics scrape must never fail on a DB hiccup
        logger.debug("metrics: outbound backlog query failed", exc_info=True)
    finally:
        db.close()

    return {
        "partner": settings.partner_name,
        "accepting_jobs": is_accepting(),
        # Queue depth / worker utilization — the two P3 explicitly names.
        "inflight_jobs": inflight_count(),
        "max_concurrent_jobs": settings.agentic_max_concurrent_runs,
        # Backpressure indicator: pending partner->NPCI sends awaiting retry.
        "outbound_retry_backlog": outbound_backlog,
        "circuit_breakers": breakers,
        "bulkheads": bulkheads,
        "context_cache": context_cache_stats(),
        "configured_boundaries": sorted(BOUNDARIES),
    }
