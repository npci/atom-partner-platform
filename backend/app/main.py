"""Partner Platform — FastAPI application."""
import logging
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.auth import seed_admin
from app.api.cert_flow_admin import router as cert_flow_admin_router
from app.api.dashboard import router as dashboard_router
from app.api.feasibility import router as feasibility_router
from app.api.users import router as users_router
from app.config import settings
from app.database import SessionLocal, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  [%(name)s]  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
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

app.include_router(auth_router)
app.include_router(cert_flow_admin_router)
app.include_router(dashboard_router)
app.include_router(feasibility_router)
app.include_router(users_router)

# A switch adapter may mount extra routes (the internal simulator plugin does, for
# its test console). Built here, at import, so a misconfigured SWITCH_ADAPTER stops
# the app starting rather than surfacing on the first NPCI message.
from app.adapters import get_switch_adapter  # noqa: E402

_adapter_router = get_switch_adapter().api_router()
if _adapter_router is not None:
    app.include_router(_adapter_router)
    logger.warning("switch adapter %s mounted extra routes at import - internal test "
                   "tooling, not for a partner deployment", type(get_switch_adapter()).__name__)


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
            # Slice 3 of A2A security hardening: validate inbound Bearer
            # JWTs signed by NPCI. Falls back to back-compat (warn-only,
            # accept unsigned) when `partner_settings.npci_jwt_secret`
            # isn't set yet.
            auth_middleware=PartnerAuthMiddleware,
            # Slice 5 of A2A security hardening: verify the X-NPCI-Signature
            # envelope before JWT decode. Falls back to warn-only when
            # `partner_settings.npci_hmac_secret` isn't installed.
            hmac_middleware=PartnerHmacMiddleware,
        )
        app.mount("/a2a-rpc", _a2a_sub_app)
        for _r in _a2a_card_routes:
            app.add_route(_r.path, _r.endpoint, methods=list(_r.methods))
        logger.info("A2A SDK mount active: /a2a-rpc/rpc")
    except Exception as _a2a_mount_err:  # noqa: BLE001
        logger.exception("A2A SDK mount failed")


@app.on_event("startup")
async def _start_txn_egress():
    # The Egress must live in THIS process: txn_state (run window, dedup,
    # request/response classification) is in-memory by design, and Ingress
    # writes it from the executor above. A separately-launched egress process
    # sees an empty window and refuses everything with NO_ACTIVE_RUN.
    if not settings.txn_egress_autostart:
        return
    if not (os.path.isfile(settings.txn_egress_ssl_cert)
            and os.path.isfile(settings.txn_egress_ssl_key)):
        logger.warning(
            "txn egress not started: TLS pair missing (%s / %s) — "
            "scripts/gen-wrapper-certs.sh generates it",
            settings.txn_egress_ssl_cert, settings.txn_egress_ssl_key,
        )
        return
    import asyncio

    import uvicorn

    from app.a2a_common.txn_egress import app as egress_app

    server = uvicorn.Server(uvicorn.Config(
        egress_app, host="0.0.0.0", port=settings.txn_egress_port,
        ssl_keyfile=settings.txn_egress_ssl_key,
        ssl_certfile=settings.txn_egress_ssl_cert,
        log_config=None,
    ))
    asyncio.create_task(server.serve())
    logger.info("txn egress serving on :%s (in-process, TLS)", settings.txn_egress_port)


@app.on_event("startup")
def on_startup():
    init_db()
    # Seed default admin user if none exists
    db = SessionLocal()
    try:
        seed_admin(db)
    finally:
        db.close()
    logger.info("Partner Platform started: %s", settings.partner_name)


@app.get("/api/health")
def health():
    return {"status": "ok", "partner": settings.partner_name}
