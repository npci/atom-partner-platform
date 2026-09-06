# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Generic A2A JSON-RPC mount.

Lifted from `certagent/cert-agent/app/a2a/mount.py` and parameterised so
any backend can supply its own AgentCard and AgentExecutor. The wiring
itself is identical across the three subsystems.

Usage (in a backend's main.py — Slice 3+):

    from app.a2a_common import build_a2a_components
    from .my_card import MY_AGENT_CARD
    from .my_executor import MyExecutor

    sub_app, card_routes = build_a2a_components(
        agent_card=MY_AGENT_CARD,
        executor=MyExecutor(),
    )
    app.mount("/a2a-rpc", sub_app)
    for r in card_routes:
        app.add_route(r.path, r.endpoint, methods=list(r.methods))

Public surface:
    build_a2a_components(agent_card, executor, task_store=None, rpc_url="/rpc")
        Returns (sub_app, card_routes).
        sub_app    — Starlette app to mount; serves POST {rpc_url}.
        card_routes — list of Starlette Route objects for
                      /.well-known/agent-card.json (and friends).
"""
from __future__ import annotations

from typing import Any, Optional, Type

from a2a.server.request_handlers.default_request_handler import LegacyRequestHandler
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from starlette.applications import Starlette
from starlette.middleware import Middleware


def build_a2a_components(
    agent_card,
    executor,
    *,
    task_store=None,
    rpc_url: str = "/rpc",
    auth_middleware: Optional[Type] = None,
    hmac_middleware: Optional[Type] = None,
    rate_limit_middleware: Optional[Type] = None,
    rate_limit_options: Optional[dict[str, Any]] = None,
):
    """Build the JSON-RPC mount + agent-card routes for a backend.

    Args:
        agent_card:  `a2a.types.a2a_pb2.AgentCard` describing this agent's
                     name / capabilities / skills / authentication schemes.
        executor:    `a2a.server.agent_execution.AgentExecutor` subclass
                     that handles incoming Tasks. Each backend writes its
                     own — see Slice 3/4.
        task_store:  Optional `TaskStore`. Defaults to `InMemoryTaskStore()`
                     (sufficient for short-running tasks). Slice 2 swaps in
                     a SQLAlchemy-backed store for durability.
        rpc_url:     Path under the mount where JSON-RPC is served.
                     Default `/rpc` matches cert-agent's existing
                     `/a2a-rpc/rpc` URL.
        auth_middleware: Optional Starlette BaseHTTPMiddleware subclass
                     wrapped around the JSON-RPC sub-app. Slice 3 of
                     the A2A security hardening passes
                     `PartnerAuthMiddleware` to require Bearer JWTs
                     signed by NPCI on inbound calls.
        hmac_middleware: Optional ASGI middleware CLASS that enforces
                     the X-NPCI-Signature envelope (Slice 5). Wrapped
                     OUTSIDE `auth_middleware` so the body buffer +
                     HMAC check happens before JWT decode.
        rate_limit_middleware: Optional ASGI middleware CLASS enforcing a
                     requests-per-second cap on the A2A ingress (Finding 10:
                     security_architecture_skills.md §4.2/§11.3). Wrapped
                     OUTSIDE `hmac_middleware` — the cheapest, earliest
                     check runs first so a flood is rejected before any
                     body buffering or crypto verification happens.
        rate_limit_options: Keyword arguments passed to
                     `rate_limit_middleware`'s constructor (e.g.
                     `{"limit_rps": 20}`).

    Returns:
        (sub_app, card_routes) — see module docstring.
    """
    if task_store is None:
        task_store = InMemoryTaskStore()
    handler = LegacyRequestHandler(
        agent_executor=executor,
        task_store=task_store,
        agent_card=agent_card,
    )
    rpc_routes = create_jsonrpc_routes(handler, rpc_url=rpc_url)
    card_routes = create_agent_card_routes(agent_card)
    # Order: rate limit outermost (cheapest check, rejects a flood before any
    # body read), then HMAC (reads body once, replays), then JWT auth
    # (header-only), then the SDK app. Starlette applies middleware in
    # list order top-down -> first item is the outermost.
    middleware: list[Middleware] = []
    if rate_limit_middleware is not None:
        middleware.append(Middleware(rate_limit_middleware, **(rate_limit_options or {})))
    if hmac_middleware is not None:
        middleware.append(Middleware(hmac_middleware))
    if auth_middleware is not None:
        middleware.append(Middleware(auth_middleware))
    sub_app = Starlette(routes=rpc_routes, middleware=middleware)
    return sub_app, card_routes
