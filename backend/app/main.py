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
from app.core.domain import get_active_pack
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

# A2A security hardening: replace `allow_origins=["*"]`
# (which is incompatible with `allow_credentials=True` per the CORS
# spec anyway) with the configured authority platform origin plus a small
# fixed dev-host allowlist. Override via `PARTNER_CORS_EXTRA_ORIGINS`
# env (comma-separated) when fronting the partner UI from a non-default
# origin during integration testing.
_cors_origins = [settings.authority_platform_url.rstrip("/")]
_extra = os.getenv("PARTNER_CORS_EXTRA_ORIGINS", "").strip()
if _extra:
    # Reject a wildcard explicitly. Starlette treats "*" in `allow_origins`
    # combined with `allow_credentials=True` by REFLECTING the request origin,
    # which is strictly worse than the spec-mandated refusal — every origin
    # becomes trusted for credentialed requests. Nothing validated this input
    # before, so a single stray character in an env var silently opened the API.
    _extra_origins = [o.strip().rstrip("/") for o in _extra.split(",") if o.strip()]
    if "*" in _extra_origins:
        raise RuntimeError(
            "PARTNER_CORS_EXTRA_ORIGINS contains '*'. With allow_credentials=True "
            "that reflects any request origin, trusting every site with the "
            "session cookie. List the origins explicitly."
        )
    _cors_origins.extend(_extra_origins)
# Local dev origins for the partner-side React app.
#
# Gated on environment, like docs_url above. These were appended
# unconditionally, so a production instance answered
# `Access-Control-Allow-Origin: http://localhost:3000` with
# `...-Credentials: true` — exploitable only by something already listening on
# the victim's own loopback, but there is no reason for production to carry it.
if not _is_prod:
    _cors_origins.extend(["http://localhost:3000", "http://localhost:5173"])

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Security response headers (SAST finding F-005) ───────────────────────────
# Sets Strict-Transport-Security on every response so browsers always use HTTPS
# for this domain. The max-age is 1 year with includeSubDomains for production;
# development uses a shorter duration so local cert rotation doesn't lock devs out.
_HSTS_MAX_AGE = 31536000 if _is_prod else 86400  # 1 year prod, 1 day dev

# The other three headers alongside it.
#
# `deploy/edge.nginx.conf` sets these at the edge and does it well, so in the
# shipped topology this is redundant — which is exactly why it was missing and
# why it should not stay missing. The backend is not always behind that edge:
# `docker-compose.override.yml` publishes it on 127.0.0.1:18001, and a host
# install may front it with something else entirely. On those paths the API
# answered with HSTS and nothing else.
#
# `frame-ancestors 'none'` rather than a full policy: this app serves JSON, not
# documents, so a restrictive default-src would be cargo-culted rather than
# load-bearing. The framing and sniffing defences are the two that matter for
# an API surface, and Referrer-Policy stops a URL leaking cross-origin.
_SECURITY_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"frame-ancestors 'none'"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
)


class SecurityHeadersMiddleware:
    """ASGI middleware adding HSTS and the baseline security headers.

    Each header is only appended when absent, so a response that already
    carries one — or an edge that sets its own — is left alone rather than
    receiving a duplicate. Duplicated CSP headers combine conjunctively, so a
    blind append could silently tighten a policy into something that blocks a
    working page.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                if b"strict-transport-security" not in present:
                    headers.append(
                        (b"strict-transport-security",
                         f"max-age={_HSTS_MAX_AGE}; includeSubDomains".encode())
                    )
                for name, value in _SECURITY_HEADERS:
                    if name not in present:
                        headers.append((name, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


# Retained under the old name so existing imports/tests keep resolving.
HSTSMiddleware = SecurityHeadersMiddleware

app.add_middleware(SecurityHeadersMiddleware)

# ── Global inbound body-size backstop ────────────────────────────────────────
# Defense-in-depth for every route OUTSIDE the A2A mount (which has its own,
# stricter, streaming-aware limit via PartnerHmacMiddleware). The limit is
# sourced from the hostility-tier registry — see core/hostility.py.
from app.core.body_size_middleware import MaxBodySizeMiddleware

app.add_middleware(MaxBodySizeMiddleware, max_bytes=settings.a2a_max_request_body_bytes)

# ── Dashboard REST throttle ──────────────────────────────────────────────────
# A2ARateLimitMiddleware is installed on the /a2a-rpc SUB-APP by mount.py, and
# Starlette middleware on a mounted sub-app does not apply to the parent — so
# every route registered below (auth, dashboard, feasibility, users) was
# unthrottled. Per-caller rather than global; see the module docstring.
from app.core.dashboard_rate_limit import DashboardRateLimitMiddleware

app.add_middleware(
    DashboardRateLimitMiddleware,
    limit=settings.dashboard_rate_limit_per_min,
    window_s=60.0,
)

app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(feasibility_router)
app.include_router(users_router)

# ITA-4 — the reverse tunnel's ingress (H3; hard-disabled unless
# integration_testing_enabled, which config.py now refuses to accept outside
# development — see the _env_is_protected() guard there. That refusal is new:
# this comment asserted it for some time while only per-request checks existed,
# so the flag really could be set in production. Registered unconditionally so
# a config flip does not change the route table shape.
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
# startup — the SDK path is unused until the authority flips this partner over.
#
# The pack is resolved HERE, outside the try, and deliberately so. The agent
# card's prose is pack-driven, so building it can raise PackError — and the
# `except Exception` below reports every failure as "a2a-sdk not importable".
# A typo'd DOMAIN_PACK would otherwise produce a green boot with no
# /a2a-rpc/rpc and no /.well-known/agent-card.json, every inbound change 404ing,
# under a log line blaming a package that is installed fine. `registry.py` is
# explicit that a bad pack must fail loudly rather than degrade; this is where
# that happens.
get_active_pack()

try:
    from app.a2a_common import build_a2a_components
    from app.a2a_common.auth_middleware import PartnerAuthMiddleware
    from app.a2a_common.hmac_middleware import PartnerHmacMiddleware
    from app.a2a_common.partner_card import get_partner_agent_card
    from app.a2a_common.partner_executor import PartnerAgentExecutor
    from app.a2a_common.rate_limit_middleware import A2ARateLimitMiddleware
except Exception as _a2a_import_err:  # noqa: BLE001
    logger.warning("a2a-sdk not importable; skipping /a2a-rpc mount", exc_info=True)
else:
    try:
        _a2a_sub_app, _a2a_card_routes = build_a2a_components(
            agent_card=get_partner_agent_card(),
            executor=PartnerAgentExecutor(),
            # In-memory store — get_task_store() swaps in later so
            # Tasks survive worker restarts.
            task_store=None,
            rpc_url="/rpc",
            # Application-layer rate limit on the A2A ingress, independent
            # of whether an operator has deployed nginx in front of the
            # stack. The limit is sourced from the hostility-tier
            # registry — see core/hostility.py.
            rate_limit_middleware=A2ARateLimitMiddleware,
            rate_limit_options={"limit_rps": settings.a2a_rate_limit_rps},
            # A2A security hardening: validate inbound Bearer
            # JWTs signed by the authority. FAIL-CLOSED when
            # `partner_settings.authority_jwt_secret` isn't configured.
            auth_middleware=PartnerAuthMiddleware,
            # A2A security hardening: verify the HMAC signature
            # envelope before JWT decode. FAIL-CLOSED when
            # `partner_settings.authority_hmac_secret` isn't installed.
            hmac_middleware=PartnerHmacMiddleware,
        )
        app.mount("/a2a-rpc", _a2a_sub_app)
        for _r in _a2a_card_routes:
            app.add_route(_r.path, _r.endpoint, methods=list(_r.methods))
        logger.info("A2A SDK mount active: /a2a-rpc/rpc")
    except Exception as _a2a_mount_err:  # noqa: BLE001
        logger.exception("A2A SDK mount failed")


def _schedule_authority_preflight() -> None:
    """Probe the authority once at startup and say plainly what is wrong.

    `run_authority_reachability_check` already layers the three diagnostics an
    operator needs — agent card reachable, api_key accepted, signed echo
    round-trip — but it was reachable ONLY by a human clicking "Test
    Connection" in Settings. A deployment that could not talk to the authority
    at all therefore booted clean and looked healthy, and the operator learned
    of it later from a symptom naming neither the cause nor the layer: a bare
    URLError, or a 401 from the far side.

    Every configuration trap this deployment actually hit is caught by one of
    those three layers:

      * AUTHORITY_PLATFORM_URL missing its port — docker fronts the backend on
        80 so the compose value carries none; copy that to a host install with
        no reverse proxy and it resolves to port 80 where nothing listens;
      * a rotated or empty partner_api_key;
      * an `authority_hmac_secret` that does not match the authority's
        `partner_agents.signing_secret`, which 401s every send.

    NON-FATAL, and off the startup path. The authority being down is not this
    platform's failure to start, and refusing to boot on it would turn a peer
    outage into a restart loop. Set PARTNER_SKIP_STARTUP_CONNECTIVITY_CHECK=1
    to suppress it (air-gapped installs, or a peer known to be down).
    """
    import os
    import threading

    if os.getenv("PARTNER_SKIP_STARTUP_CONNECTIVITY_CHECK", "").strip().lower() in (
            "1", "true", "yes"):
        logger.info("Authority connectivity preflight skipped by configuration.")
        return

    def _probe() -> None:
        try:
            from app.authority_client import run_authority_reachability_check

            db = SessionLocal()
            try:
                result = run_authority_reachability_check(db)
            finally:
                db.close()

            message = str(result.get("message") or result)
            if result.get("status") == "ok":
                logger.info("Authority connectivity preflight OK — %s", message)
                return

            # `run_authority_reachability_check` short-circuits on the first failing
            # layer, and most of its messages ALREADY name the remedy (the SSRF
            # refusal names AUTHORITY_SSRF_ALLOWED_HOSTS, the auth failure names
            # the api key). Adding a guess on top of one of those actively
            # misleads: the first run of this preflight appended "check the
            # PORT" to an SSRF refusal, which is a different problem with a
            # different fix.
            #
            # So only add a hint where the message names no remedy of its own,
            # and only for the one failure whose text is genuinely uninformative
            # — a bare connection failure, which on a host install is almost
            # always the missing port.
            low = message.lower()
            names_remedy = any(t in low for t in (
                "ssrf_allowed_hosts", "allow_private_networks", "api key",
                "api_key", "hmac", "signing_secret", "set ", "add the host",
            ))
            if not names_remedy and ("cannot reach" in low or "connect" in low
                                     or "timed out" in low):
                hint = (" Most often on a host install this is a missing PORT in "
                        "AUTHORITY_PLATFORM_URL: the docker value omits it "
                        "because nginx fronts the backend on 80, and a host "
                        "install with no reverse proxy has nothing there.")
            elif names_remedy:
                hint = ""          # the message is already actionable
            else:
                hint = " See Settings -> Test Connection for the full diagnostic."

            logger.error(
                "AUTHORITY CONNECTIVITY PREFLIGHT FAILED: %s%s This platform has "
                "started and serves its UI, but outbound A2A will not work until "
                "this is fixed.",
                message, hint,
            )
        except Exception:  # noqa: BLE001 — a diagnostic must never break boot
            logger.warning(
                "Authority connectivity preflight could not run", exc_info=True)

    threading.Thread(target=_probe, name="authority-preflight", daemon=True).start()


@app.on_event("startup")
def on_startup():
    # Fail fast on unsafe/missing hostility-tier configuration BEFORE the DB
    # is touched or any traffic is accepted — startup validation rule. See
    # core/hostility.py.
    from app.core.hostility import validate_at_startup
    validate_at_startup()
    # Refuse to boot on a PARTNER_SECRET_KEK that is present but cannot work.
    # Without this the platform started clean on an unusable KEK and only failed
    # when an operator saved their first credential, as a generic 500 whose
    # cause was visible only in the container log. See core/secret_box.py and
    # F-25. An UNSET KEK warns rather than aborts — compose defaults it empty.
    from app.core.secret_box import validate_at_startup as validate_secret_box
    validate_secret_box()
    # Refuse a multi-worker boot: A2A rate limiting and the revision-context
    # cache are per-process, so extra workers silently multiply the effective
    # rate limit. See core/runtime.py for the override and
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

    # Data retention sweep — daily background purge of superseded
    # generated-code iterations and stale agent-run payloads. See
    # services/retention.py.
    from app.services import retention_scheduler
    retention_scheduler.start()

    # Outbound A2A retry sweep (the DLQ and replay process) — drains queued
    # OutboundA2ARetry rows on a short interval. See services/outbound_retry.py.
    from app.services import outbound_retry_scheduler
    outbound_retry_scheduler.start()

    _schedule_authority_preflight()

    logger.info("Partner Platform started: %s", settings.partner_name)


@app.on_event("shutdown")
def on_shutdown():
    """Graceful drain — safe scale-in with drain/linger behavior.

    Order matters: stop admitting new jobs and let in-flight ones
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
    """Readiness — distinct from liveness (safe scale-in with drain/linger
    behavior).

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
    """Application-level metrics for autoscaling decisions: scaling rules that
    depend only on CPU/memory without application metrics are a smell — queue
    depth, worker utilization, and saturation indicators are what an
    autoscaler actually needs.

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
        # Backpressure indicator: pending partner->authority sends awaiting retry.
        "outbound_retry_backlog": outbound_backlog,
        "circuit_breakers": breakers,
        "bulkheads": bulkheads,
        "context_cache": context_cache_stats(),
        "configured_boundaries": sorted(BOUNDARIES),
    }
