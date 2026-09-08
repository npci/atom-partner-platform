# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Partner platform AgentCard.

Lists the four skills the partner platform participates in:

  Inbound (NPCI → partner — handled by `PartnerAgentExecutor`):
    * change_communication      Receive a UPI feature change + Product Kit
    * clarification_response    Receive an answer to a previously-submitted query

  Outbound (partner → NPCI — sent via `app.a2a_common.client.send_a2a_message`):
    * query                     Submit an implementation question
    * progress                  Report a ProgressStep
    * readiness                 Declare readiness for certification

A2A AgentCard skills describe what the agent CAN DO from a discovery
standpoint — they don't distinguish inbound vs outbound. We list all
four because a partner may both receive change_communications and emit
queries / progress reports.

The legacy hand-rolled `app.api.a2a.get_agent_card()` dict has been
deleted. This typed protobuf object is now the only agent card the partner
platform serves — the SDK exposes it at
`/.well-known/agent-card.json` from the routes registered in
`app.main`.
"""
from __future__ import annotations

from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)

from app.config import settings

# A2A security hardening — partner side advertises a single bearer_jwt scheme
# (HS256) signed with the partner's per-partner `authority_jwt_secret`. mTLS
# isn't on the partner ingress: this stack receives in one direction only, and
# the counterparty-tier check happens on the authority's ingress.
#
# Built per-call rather than as a module constant because this description is
# SERVED in the public agent card, so it has to come from the pack like the rest
# of the card. It previously read "signed by NPCI ... on NPCI" in the same
# sentence as the already-renamed `partner_settings.authority_jwt_secret` — a
# half-finished rename visible to every peer that fetched the card.
def _security_schemes(authority: str) -> dict:
    return {
        "bearer_jwt": SecurityScheme(
            http_auth_security_scheme=HTTPAuthSecurityScheme(
                description=(
                    f"HS256 JWT signed by {authority} with this partner's "
                    f"per-partner signing secret (stored on both sides: "
                    f"`partner_agents.jwt_signing_secret` on the authority, "
                    f"`partner_settings.authority_jwt_secret` here). Validated "
                    f"by the partner-side auth middleware."
                ),
                scheme="bearer",
                bearer_format="JWT",
            ),
        ),
    }

_SECURITY_REQUIREMENTS = [
    SecurityRequirement(schemes={"bearer_jwt": StringList(list=[])}),
]


def _sentence_case(s: str) -> str:
    """Upper-case the first character only, leaving the rest untouched."""
    return s[:1].upper() + s[1:]


def _build_partner_agent_card() -> AgentCard:
    """Build the AgentCard at import time. Pulled into a function so
    `settings.partner_name` is resolved when this module loads — same
    pattern as `get_agent_card()` in the legacy router.

    The prose is pack-driven because this card is SERVED PUBLICLY at
    `/.well-known/agent-card.json`, to any peer that discovers this platform. It
    described itself as a "UPI ecosystem partner agent" receiving changes "from
    NPCI" regardless of deployment, which made the most externally-visible
    surface in the system also the most brand-specific one.
    """
    from app.core.domain import get_active_pack

    _pack = get_active_pack()
    return AgentCard(
        name=settings.partner_name,
        # `_sentence_case`, not str.capitalize(): the latter lower-cases
        # everything after the first character, mangling the acronyms a pack
        # deliberately supplies in upper case.
        description=_sentence_case(
            f"{_pack.prompt_block('partner_descriptor', 'an ecosystem partner')} "
            f"agent. Receives change communications and clarification responses "
            f"from {_pack.prompt_block('authority', 'the authority')}; emits "
            f"implementation queries, progress reports and readiness "
            f"declarations."
        ),
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=False),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        security_schemes=_security_schemes(
            _pack.prompt_block("authority", "the authority")),
        security_requirements=_SECURITY_REQUIREMENTS,
        supported_interfaces=[
            AgentInterface(
                # Full URL — the A2A SDK posts here verbatim, so a bare
                # path won't work (httpx requires the http:// prefix).
                # Sourced from `settings.partner_public_url` which
                # defaults to the docker service name; production
                # overrides via PARTNER_PUBLIC_URL env.
                url=f"{settings.partner_public_url.rstrip('/')}/a2a-rpc/rpc",
                protocol_binding="JSONRPC",
                protocol_version="1.0",
            ),
        ],
        skills=[
            AgentSkill(
                id="change_communication",
                name="Receive Change Communication",
                description=(
                    f"Receive "
                    f"{_pack.prompt_block('change_descriptor', 'a change')} from "
                    f"{_pack.prompt_block('authority', 'the authority')}. Data: "
                    "{change_id, payload: {title, initial_prompt, "
                    "enhanced_prompt, documents: [{type, name, content, "
                    "format}]}}. Idempotent on `payload.change_id`; "
                    "duplicates are accepted but skipped."
                ),
                tags=["change", "inbound"],
            ),
            AgentSkill(
                id="clarification_response",
                name="Receive Clarification Response",
                description=(
                    "Receive an answer to a previously-submitted query. "
                    "Data: {payload: {change_id, response}}. Updates the "
                    "matching `OutgoingQuery` row."
                ),
                tags=["change", "negotiation", "inbound"],
            ),
            AgentSkill(
                id="query",
                name="Submit Implementation Query",
                description=(
                    f"Outbound: send an implementation question to "
                    f"{_pack.prompt_block('authority', 'the authority')} about a "
                    f"received change. "
                    f"{_pack.prompt_block('authority_cap', 'The authority')} "
                    f"auto-drafts a response asynchronously and a PO approves "
                    f"before delivery."
                ),
                tags=["change", "negotiation", "outbound"],
            ),
            AgentSkill(
                id="progress",
                name="Report Implementation Progress",
                description=(
                    "Outbound: report a ProgressStep "
                    "(design_completed | coding_completed | testing_completed). "
                    "Triggers automatic ChangePartnerAssignment.status update."
                ),
                tags=["change", "lifecycle", "outbound"],
            ),
            AgentSkill(
                id="readiness",
                name="Declare Readiness",
                description=(
                    "Outbound: declare ready for certification. Requires "
                    "all three ProgressSteps reported first."
                ),
                tags=["change", "lifecycle", "outbound"],
            ),
        ],
    )


# LAZY, not module-level. Building this at import stopped being "safe — no I/O"
# the moment the card's prose became pack-driven: `get_active_pack()` reads and
# parses a YAML file, and raises PackError when DOMAIN_PACK is wrong.
#
# `main.py` imports this module inside a `try: … except Exception:` whose only
# log line says "a2a-sdk not importable". A module-level build therefore turned
# a typo'd DOMAIN_PACK into a GREEN BOOT with no /a2a-rpc/rpc and no
# /.well-known/agent-card.json — every inbound change 404s — while blaming a
# missing SDK. That is precisely the failure `core/domain/registry.py` says must
# never be softened into a fallback.
#
# It also froze whichever pack happened to be active when some other module
# first pulled this one into sys.modules — order-dependent under pytest, where
# several suites monkeypatch DOMAIN_PACK.
#
# Deliberately NOT memoised here. `get_active_pack()` is already cached per path
# in the registry, so the only uncached work is protobuf construction, and this
# is called once at mount time. A second cache would be a second thing for
# `registry.clear_cache()` to fail to reset — reintroducing the staleness this
# comment exists to describe.
def get_partner_agent_card() -> AgentCard:
    """The served AgentCard, built from the pack active at call time."""
    return _build_partner_agent_card()
