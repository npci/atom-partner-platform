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

Slice 8 deleted the legacy hand-rolled `app.api.a2a.get_agent_card()`
dict. This typed protobuf object is now the only agent card the partner
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

# Slice 4 of A2A security hardening — partner side advertises a single
# bearer_jwt scheme (HS256) signed with the partner's per-partner
# `npci_jwt_secret`. mTLS isn't on the partner ingress (the partner
# stack is the receiver of bank certs in only one direction — NPCI's
# bank-tier check happens on the NPCI ingress).
_SECURITY_SCHEMES = {
    "bearer_jwt": SecurityScheme(
        http_auth_security_scheme=HTTPAuthSecurityScheme(
            description=(
                "HS256 JWT signed by NPCI with this partner's per-partner "
                "signing secret (stored on both sides: "
                "`partner_agents.jwt_signing_secret` on NPCI, "
                "`partner_settings.npci_jwt_secret` here). Validated by "
                "the partner-side auth middleware."
            ),
            scheme="bearer",
            bearer_format="JWT",
        ),
    ),
}

_SECURITY_REQUIREMENTS = [
    SecurityRequirement(schemes={"bearer_jwt": StringList(list=[])}),
]


def _build_partner_agent_card() -> AgentCard:
    """Build the AgentCard at import time. Pulled into a function so
    `settings.partner_name` is resolved when this module loads — same
    pattern as `get_agent_card()` in the legacy router."""
    return AgentCard(
        name=settings.partner_name,
        description=(
            "UPI ecosystem partner agent. Receives change communications "
            "and clarification responses from NPCI; emits implementation "
            "queries, progress reports and readiness declarations."
        ),
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=False),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        security_schemes=_SECURITY_SCHEMES,
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
                    "Receive a new UPI change from NPCI. Data: "
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
                    "Outbound: send an implementation question to NPCI "
                    "about a received change. NPCI auto-drafts a response "
                    "asynchronously and a PO approves before delivery."
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


# Built once at module import; safe — no I/O, just protobuf construction.
PARTNER_AGENT_CARD: AgentCard = _build_partner_agent_card()
