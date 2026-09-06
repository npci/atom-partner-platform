# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Generic outbound A2A client.

Wraps `a2a.client.ClientFactory` so any backend can send a Task to a
remote agent (partner / cert-agent / NPCI) by URL, with optional Bearer
auth. Cert-agent's outbound code (`certagent/cert-agent/app/a2a/client.py`)
remains the reference for cert-specific helpers; this module is the
generic primitive used by `services/a2a_client.py` after Slice 5.

Slice 1 ships only `send_a2a_message`. Streaming, multi-turn, and
push-notification variants land alongside the per-backend Executors.
"""
from __future__ import annotations

import uuid
from typing import Optional

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.types.a2a_pb2 import Message, Part, Role, SendMessageRequest
from google.protobuf import json_format, struct_pb2


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


def _extract_artifact_dict(event) -> Optional[dict]:
    """Pull a structured-Part dict out of any A2A SDK event (ITA-3 / B2).

    Ported from the NPCI backend's client, which has captured replies this way
    since Slice 25 — the two files legitimately differ per service, but the
    SDK event shapes do not. With streaming=False the SDK yields a
    StreamResponse wrapping the payload in a oneof (task / message /
    status_update / artifact_update); the Task carrying `.artifacts` lives
    INSIDE that oneof — without the unwrap every receiver reply reads as None.
    """
    inner = event
    for _field in ("task", "message", "status_update", "artifact_update"):
        try:
            if event.HasField(_field):
                inner = getattr(event, _field)
                break
        except (ValueError, AttributeError):
            continue

    candidates: list = []
    artifacts = getattr(inner, "artifacts", None) or []
    for art in artifacts:
        candidates.extend(getattr(art, "parts", None) or [])
    one_artifact = getattr(inner, "artifact", None)
    if one_artifact is not None:
        candidates.extend(getattr(one_artifact, "parts", None) or [])
    if hasattr(inner, "parts"):
        candidates.extend(getattr(inner, "parts", None) or [])

    for part in candidates:
        try:
            if part.HasField("data") and part.data.HasField("struct_value"):
                return json_format.MessageToDict(part.data.struct_value)
        except Exception:  # noqa: BLE001 — defensive against unknown shapes
            continue
    return None


async def send_a2a_message(
    base_url: str,
    *,
    context_id: str,
    task_id: str,
    data: dict,
    auth_header: Optional[str] = None,
    hmac_secret: Optional[str] = None,
    correlation_id: Optional[str] = None,
    timeout: float = 30.0,
) -> Optional[dict]:
    """Send a single A2A message to a remote agent and return its reply body.

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
        correlation_id: Optional value sent as `X-NPCI-Correlation-ID` (and,
                     when `data` carries a `change_id`, `X-NPCI-Change-ID`)
                     so the receiver's logs/telemetry can be traced back to
                     the originating job/thread (Finding 13:
                     security_architecture_skills.md §13.1). Distinct from
                     the ENVELOPE's own `correlation_id` field (which
                     `data` already carries where applicable) — this is the
                     transport-layer header a proxy/log pipeline can filter
                     on without parsing the JSON-RPC body.
        timeout:     httpx client timeout in seconds. 30s is a reasonable
                     default for synchronous task acceptance; longer
                     calls should switch to streaming.

    Returns:
        The receiver's structured reply as a dict — for NPCI, the
        `a2a-task-receipt` artifact (since ITA-2 possibly carrying a merged
        handler dict, e.g. a structured `http_exchange_response`) — or None
        when the receiver emitted no structured artifact. Until ITA-3 this
        layer drained the stream and returned None; the tunnel's reverse
        direction needs the body to actually come home (blocker B2).
    """
    headers: dict[str, str] = {}
    if auth_header:
        headers["Authorization"] = auth_header
    if correlation_id:
        headers["X-NPCI-Correlation-ID"] = correlation_id
    change_id_for_header = data.get("change_id") if isinstance(data, dict) else None
    if change_id_for_header:
        headers["X-NPCI-Change-ID"] = str(change_id_for_header)

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
    ) as http:
        factory = ClientFactory(ClientConfig(httpx_client=http, streaming=False))
        client = await factory.create_from_url(base_url)
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
        # Capture the LAST structured artifact while draining — the receipt
        # is emitted last (after any incremental events), and "last wins"
        # matches the NPCI client's behaviour for the same stream.
        last_response: Optional[dict] = None
        async for event in client.send_message(req):
            artifact = _extract_artifact_dict(event)
            if artifact is not None:
                last_response = artifact
        return last_response
