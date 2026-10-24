"""Generic outbound A2A client.

Wraps `a2a.client.ClientFactory` so a backend can send a Task to a remote agent by
URL with optional Bearer auth + HMAC envelope.

Two differences from NPCI's `backend/app/a2a_common/client.py`, both deliberate —
a fix to this primitive should be considered for both copies:

1. It returns the receiver's reply instead of `None`. The Txn Egress needs the
   ACK that rides back in the same synchronous A2A reply, and an operator hitting
   a readiness/declare call wants to see what NPCI said. Existing callers that
   only care that delivery succeeded simply ignore the return value.

2. It moves the RPC URL the agent card advertises onto `base_url` (see
   `_rebase_to_base_url`). The card was fetched from `base_url`, so that host is
   reachable by definition; the host the card advertises is whatever NPCI's
   `npci_public_url` was set to, which is a docker service name that does not
   resolve from a partner running outside NPCI's compose network.
"""
from __future__ import annotations

import functools
import logging
import time
import uuid
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.types.a2a_pb2 import Message, Part, Role, SendMessageRequest
from google.protobuf import json_format, struct_pb2

logger = logging.getLogger("partner.a2a")


def _dict_to_part(payload: dict) -> Part:
    """Wrap a flat dict as a structured `Part` (the SDK's payload primitive).

    A2A `Message`s are composed of `Part`s; structured data goes in a
    `data` part backed by google.protobuf.Struct. JSON-serialisable dicts
    map cleanly via `json_format.ParseDict`.
    """
    s = struct_pb2.Struct()
    json_format.ParseDict(payload, s)
    v = struct_pb2.Value()
    v.struct_value.CopyFrom(s)
    part = Part()
    part.data.CopyFrom(v)
    return part


def _rebase_to_base_url(url: str, base_url: str) -> str:
    """Move `url` onto `base_url`'s scheme + host, keeping its path and query.

    The card's PATH is authoritative — NPCI serves the RPC at `/a2a-rpc/rpc`
    and only NPCI knows that — while its HOST is whatever `npci_public_url` was
    set to on the NPCI side, which is `http://npci_backend:8000` unless someone
    overrode it. That default is correct for every peer inside the compose
    network and unroutable from anywhere else, and one card serves both, so the
    receiver cannot publish a value that satisfies everyone. Rebasing keeps the
    host we were configured to reach and takes the path from the card.
    """
    base, target = urlsplit(base_url), urlsplit(url)
    if not base.netloc:
        return url
    return urlunsplit((
        base.scheme or target.scheme, base.netloc,
        target.path, target.query, target.fragment,
    ))


@functools.lru_cache(maxsize=1)
def _ssl_context():
    """One TLS context for every outbound client.

    Building it loads the system CA store (about a second on a Windows host) and
    a client used to be built per message. The context itself is reusable
    across clients and event loops."""
    return httpx.create_ssl_context()


# base_url -> (expires_at, rebased card). The card changes only when NPCI is
# redeployed, so fetching it for every message is a wasted round-trip.
_CARD_TTL_SECONDS = 300.0
_cards: dict = {}


async def _agent_card(http: "httpx.AsyncClient", base_url: str):
    hit = _cards.get(base_url)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    card = await A2ACardResolver(http, base_url).get_agent_card()
    for interface in card.supported_interfaces:
        moved = _rebase_to_base_url(interface.url, base_url)
        if moved != interface.url:
            # The one line that makes a card/URL mismatch visible — without
            # it the send just fails to connect to a host nothing logged.
            logger.info("agent card RPC url %s -> %s", interface.url, moved)
            interface.url = moved
    _cards[base_url] = (time.monotonic() + _CARD_TTL_SECONDS, card)
    return card


async def send_a2a_message(
    base_url: str,
    *,
    context_id: str,
    task_id: str,
    data: dict,
    auth_header: Optional[str] = None,
    hmac_secret: Optional[str] = None,
    timeout: float = 30.0,
) -> dict:
    """Send a single A2A message to a remote agent and return its reply.

    Args:
        base_url:    Remote agent's host (e.g. `http://cert-agent:8000`
                     or `https://partner.example.com`). The SDK appends
                     `/.well-known/agent-card.json` to discover the RPC
                     endpoint, so pass the bare host — NOT the `/a2a-rpc`
                     subpath.
        context_id:  Conversational thread key — typically a
                     `change_request_id` or cert `run_id`.
        task_id:     Idempotency / correlation id. Reuse across retries
                     so the remote can dedupe.
        data:        JSON-serialisable dict; sent as a structured Part.
        auth_header: Optional `Authorization` header (e.g. "Bearer <jwt>").
                     Plug `fetch_bearer_jwt(...)` from auth.py here when
                     calling cert-agent or any partner that requires JWT.
        timeout:     httpx client timeout in seconds. 30s is a reasonable
                     default for synchronous task acceptance; longer
                     calls should switch to streaming.

    Returns:
        The structured data Part of the receiver's final artifact, or `{}`
        if it emitted none. See the divergence note in the module docstring.
    """
    headers: dict[str, str] = {}
    if auth_header:
        headers["Authorization"] = auth_header

    # Slice 5 outbound HMAC envelope — partner→NPCI direction. When
    # the partner has installed `partner_settings.npci_hmac_secret`,
    # callers pass it here to sign outbound bodies. The hook fires
    # after httpx serialises the SDK's JSON-RPC body, so we sign the
    # actual wire bytes the receiver will hash.
    event_hooks: dict[str, list] = {}
    if hmac_secret:
        from .hmac_signer import sign as _hmac_sign

        async def _attach_hmac(request: "httpx.Request") -> None:
            envelope = _hmac_sign(request.content or b"", hmac_secret)
            for k, v in envelope.items():
                request.headers[k] = v

        event_hooks["request"] = [_attach_hmac]

    async with httpx.AsyncClient(
        timeout=timeout,
        headers=headers,
        event_hooks=event_hooks or None,
        verify=_ssl_context(),
    ) as http:
        factory = ClientFactory(ClientConfig(httpx_client=http, streaming=False))
        # `create_from_url` would resolve the card and post to whatever host it
        # advertises; resolve it here instead so the URL can be rebased first.
        card = await _agent_card(http, base_url)
        client = factory.create(card)
        # task_id on Message means "continue this EXISTING task on the
        # receiver" per the A2A spec. For first sends we omit it so
        # NPCI allocates a new task; the caller's `task_id` arg stays
        # for local audit/correlation only. Same fix as backend/.../client.py.
        req = SendMessageRequest(
            message=Message(
                role=Role.ROLE_USER,
                message_id=str(uuid.uuid4()),
                context_id=context_id,
                parts=[_dict_to_part(data)],
            )
        )
        # The stream carries StreamResponse events; the completed Task (with
        # the receiver's artifact) rides on the `task` field. Later events
        # overwrite earlier ones so the LAST artifact wins, which is the final
        # state rather than an intermediate one.
        reply: dict = {}
        try:
            async for ev in client.send_message(req):
                if not ev.HasField("task"):
                    continue
                for artifact in ev.task.artifacts:
                    for part in artifact.parts:
                        if part.HasField("data"):
                            reply = json_format.MessageToDict(part.data.struct_value)
        except Exception:
            _cards.pop(base_url, None)   # a failed send may mean a stale card
            raise
        return reply
