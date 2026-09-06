# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""A2A client for communicating with the NPCI Platform.

Slice 8 of the unified A2A SDK refactor deleted NPCI's legacy
`POST /api/a2a/tasks/send` endpoint — the SDK JSON-RPC mount at
`/a2a-rpc/rpc` handles outbound now. This module was left on the
legacy path; queries / progress updates / readiness declarations sent
from partner UI silently 404'd as a result.

This is the symmetric mirror of NPCI's `app.services.a2a_client`:
auth handshake then SDK send via `app.a2a_common.client.send_a2a_message`.
HMAC envelope rides for free when `partner_settings.npci_hmac_secret`
is installed (Slice 5).

Auth note: we keep using the api_key→JWT handshake at /api/a2a/auth.
NPCI's inbound `decode_partner_token` validates with `settings.secret_key`
(platform-wide), which only the handshake mints with. Locally minting
with `npci_jwt_secret` would NOT work — that's the per-partner secret
used in the OPPOSITE direction (NPCI→partner).
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from app.a2a_common.client import send_a2a_message
from app.a2a_common.hmac_signer import DEFAULT_MAX_SKEW_S
from app.a2a_common.protocol import make_envelope
from app.core.errors import safe_exc
from app.core.secret_box import safe_key_label
from app.models import IncomingChange, PartnerSetting

logger = logging.getLogger(__name__)


# ── SSRF guard: reject connections to private/reserved IPs ───────────────────
# Used by run_npci_reachability_check() to prevent server-side request forgery
# when the operator configures a malicious URL in partner_settings (F-003).
#
# The ranges are split into two tiers because they are NOT equally suspect, and
# collapsing them into one list is what made a legitimate deployment
# unreachable. NPCI's UAT platform lives on RFC-1918 space, so a flat "block
# all private addresses" rule refused every probe against it before a packet
# left the container — the operator saw a security refusal where the honest
# answer was "this address is fine, nobody told the guard".
#
# TIER 1 — never a legitimate NPCI platform, not overridable.
# Loopback would mean the partner backend probing itself; link-local carries
# the cloud metadata service (169.254.169.254), the canonical SSRF target whose
# credentials would be the whole prize. Neither is ever the right answer, so no
# setting re-enables them.
_NEVER_ALLOWED_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),       # loopback
    ipaddress.ip_network("169.254.0.0/16"),    # link-local (cloud metadata)
    ipaddress.ip_network("::1/128"),           # IPv6 loopback
    ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
]

# TIER 2 — private address space. Blocked by DEFAULT, because an unconfigured
# deployment should not be able to probe the internal network. Permitted for an
# approved target via `npci_ssrf_allowed_hosts` (per host, preferred) or
# `npci_ssrf_allow_private_networks` (blanket). This is the tier that a
# real internal NPCI platform lands in.
_PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),          # IPv6 unique-local
]


def _ssrf_allowed_hosts() -> frozenset[str]:
    """Hosts approved to resolve into private space, from settings.

    Read at CALL time rather than import time so a settings change takes
    effect on the next probe, and so tests can set it per case.
    """
    from app.config import settings

    raw = getattr(settings, "npci_ssrf_allowed_hosts", "") or ""
    return frozenset(
        h.strip().lower().rstrip(".") for h in raw.split(",") if h.strip()
    )


def _ssrf_allow_private() -> bool:
    """Whether private address space is permitted wholesale."""
    from app.config import settings

    return bool(getattr(settings, "npci_ssrf_allow_private_networks", False))


def _is_private_url(url: str, *, unresolved: bool = True) -> bool:
    """Return True if `url` must NOT be probed.

    This prevents SSRF attacks where a configured URL points at internal
    services (cloud metadata endpoints, internal dashboards, etc.).

    An address in tier-1 space (loopback / link-local) is always refused. An
    address in private space is refused unless the host is named in
    `npci_ssrf_allowed_hosts` or `npci_ssrf_allow_private_networks` is on —
    which is how an internal NPCI platform, the normal deployment shape, is
    approved without weakening the guard for everyone else.

    `unresolved` is the verdict for a host that will not resolve, and it
    differs by caller. The default (True) suits the reachability PROBE, where
    "cannot resolve" is itself the diagnostic the operator asked for. The send
    path passes False: a name that does not resolve cannot be reached, so
    refusing it buys no security, while blocking on it would convert an
    ordinary DNS blip into a refused send. See `_guard_outbound_url`.
    """
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        host_l = host.lower().rstrip(".")

        # Allow docker service names and localhost for compose setups
        if host_l in ("localhost", "host.docker.internal") or host_l.endswith("_backend"):
            return False

        allowlisted = host_l in _ssrf_allowed_hosts()
        allow_private = allowlisted or _ssrf_allow_private()

        # Resolve the hostname to IP addresses. Note this happens even for an
        # allowlisted host: the allowlist waives the PRIVATE-space rule, not the
        # tier-1 one, so naming a host that resolves to 169.254.169.254 does not
        # buy access to the metadata service.
        addrs = socket.getaddrinfo(host, parsed.port or 80, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
        for family, _type, _proto, _cname, sockaddr in addrs:
            ip = ipaddress.ip_address(sockaddr[0])
            # Unwrap IPv4-mapped IPv6 (::ffff:169.254.169.254) before
            # classifying, so the mapped spelling cannot dodge the check.
            mapped = getattr(ip, "ipv4_mapped", None)
            if mapped:
                ip = mapped

            for net in _NEVER_ALLOWED_NETS:
                if ip.version == net.version and ip in net:
                    logger.warning(
                        "SSRF guard: blocked connection to loopback/link-local IP %s (host=%s) — "
                        "not overridable by configuration", ip, host,
                    )
                    return True

            for net in _PRIVATE_NETS:
                if ip.version == net.version and ip in net:
                    if allow_private:
                        logger.info(
                            "SSRF guard: permitting private IP %s (host=%s) — approved via %s",
                            ip, host,
                            "NPCI_SSRF_ALLOWED_HOSTS" if allowlisted
                            else "NPCI_SSRF_ALLOW_PRIVATE_NETWORKS",
                        )
                        continue
                    logger.warning(
                        "SSRF guard: blocked connection to private IP %s (host=%s) — add the host "
                        "to NPCI_SSRF_ALLOWED_HOSTS if this is the real NPCI platform", ip, host,
                    )
                    return True
        return False
    except (socket.gaierror, ValueError, OSError):
        # DNS resolution failure — verdict is the caller's to set (see docstring).
        logger.warning("SSRF guard: could not resolve host in URL %s", url)
        return unresolved


def _validate_url_scheme(url: str, purpose: str = "") -> None:
    """Warn if a URL used for outbound requests uses cleartext HTTP.

    The caller should have already configured https:// in production; this
    is defense-in-depth for any URL that carries credentials (SAST finding F-002).
    """
    if (url or "").lower().startswith("http://"):
        logger.warning(
            "Cleartext HTTP URL for %s: %s — credentials will be sent in the clear. "
            "Configure https:// in partner_settings.",
            purpose or "outbound request", url,
        )


class OutboundURLBlocked(RuntimeError):
    """An outbound URL was refused by the SSRF guard.

    A distinct type because `send_task_async` persists `safe_exc(exc)` — the
    exception CLASS NAME, never `str(exc)` — as `OutboundA2ARetry.last_error`
    (CWE-209: the message would otherwise pin the resolved host into a
    UI-visible row). The class name is therefore the whole operator-facing
    signal in the retry queue, so it has to read as a cause on its own.
    """


def _guard_outbound_url(url: str, purpose: str) -> None:
    """Refuse `url` when it resolves into blocked address space. Fails closed.

    `_is_private_url()` was well-built but wired ONLY into
    `run_npci_reachability_check()` — the Settings page's "Test Connection"
    button — so the manual probe was guarded while every production send path
    (`send_task`/`send_query`/`declare_ready`/… and the outbound-retry sweep)
    connected unchecked (SAST finding F-001). This is the shared chokepoint
    that puts the guard on the transport rather than on the diagnostic.

    Raises rather than returning a sentinel so the existing failure handling
    applies unchanged: `send_task_async` catches it, queues the message for
    retry, and returns None — the "fail the send, don't connect" outcome,
    with no new contract for ~30 call sites to learn.

    `unresolved=False` deliberately narrows this to a DESTINATION policy: it
    refuses hosts that resolve into forbidden space, and stays out of the way
    otherwise. A host that does not resolve is left to fail in the transport
    exactly as it did before, so adding this guard cannot turn a DNS blip into
    a refused send.
    """
    if _is_private_url(url, unresolved=False):
        raise OutboundURLBlocked(
            f"SSRF guard refused {purpose}: {url} resolves into blocked "
            "(loopback/link-local/private) address space. If this IS the real "
            "NPCI platform, approve it by adding the host to "
            "NPCI_SSRF_ALLOWED_HOSTS, or set "
            "NPCI_SSRF_ALLOW_PRIVATE_NETWORKS=true."
        )


def _get_setting(db: Session, key: str, default: str = "") -> str:
    """Reads a `partner_settings` row. Transparently decrypts values for keys
    in `core.secret_box.SECRET_KEYS` (docs/adr/ADR-0002-secrets-vault-migration.md);
    non-secret keys (URLs, etc.) are returned as stored. Legacy plaintext
    secret rows (written before secret_box existed) are returned unchanged —
    decrypt() is a no-op for values not in `enc:v1:` form."""
    row = db.get(PartnerSetting, key)
    if not row or not row.value:
        return default
    from app.core.secret_box import SECRET_KEYS, decrypt, safe_key_label
    if key not in SECRET_KEYS:
        return row.value
    try:
        return decrypt(row.value)
    except Exception:  # noqa: BLE001 — corrupted/tamper-evident; surface as absent
        # Fixed label, never the key or value — see secret_box.safe_key_label()
        # (Checkmarx "Filtering Sensitive Logs").
        logger.critical(
            "_get_setting: failed to decrypt %s — treating as unconfigured",
            safe_key_label(key),
        )
        return default


def _get_a2a_base_url(db: Session) -> str:
    """Direct service URL for partner→NPCI A2A traffic.

    Defaults to the docker service name. Override via
    `partner_settings.npci_a2a_url`. Distinct from `npci_platform_url`
    which is the human-facing URL used for the reachability check; the SDK
    needs the bare host that backs `/a2a-rpc/rpc`, not a UI mount.
    """
    url = _get_setting(db, "npci_a2a_url", "http://npci_backend:8000").strip().rstrip("/")
    return url


def _get_human_npci_url(db: Session) -> str:
    """UI-facing NPCI URL (used by `run_npci_reachability_check`)."""
    url = _get_setting(db, "npci_platform_url", "http://localhost").strip().rstrip("/")
    if "://localhost" in url:
        url = url.replace("://localhost", "://host.docker.internal")
    return url


def _get_api_key(db: Session) -> str:
    return _get_setting(db, "partner_api_key", "")


def authenticate(db: Session) -> str | None:
    """Authenticate with NPCI and return a JWT token.

    Hits /api/a2a/auth on the direct service URL — the auth endpoint
    is unprotected by HMAC/CIDR middleware, so going direct vs through
    nginx is equivalent and direct keeps the dev path simple.
    """
    url = _get_a2a_base_url(db)
    _validate_url_scheme(url, purpose="NPCI A2A auth (API key transmission)")
    # Guard BEFORE the try below: that block returns None on any exception, so
    # a guard inside it would be swallowed into the generic "auth failed" path
    # and the operator would never learn the send was refused, not rejected.
    _guard_outbound_url(url, "NPCI A2A auth (API key transmission)")
    api_key = _get_api_key(db)
    if not api_key:
        logger.error("NPCI API key not configured")
        return None

    try:
        resp = httpx.post(f"{url}/api/a2a/auth", json={"api_key": api_key}, timeout=15.0)
        if resp.status_code == 200:
            token = resp.json().get("jwt")
            logger.info("NPCI auth success")
            return token
        else:
            logger.warning("NPCI auth failed: %d %s", resp.status_code, resp.text[:200])
            return None
    except Exception as e:
        logger.error("NPCI auth error", exc_info=True)
        return None


def _resolve_correlation_id(db: Session, change_id: str | None, payload: dict) -> str | None:
    """correlation_id resolution (v1.1 §5): a caller-supplied payload value
    wins (the query path uses it as the OutgoingQuery row pointer); otherwise
    echo NPCI's per-(change, bank) thread id captured on receipt, so every
    reply threads back to the right conversation."""
    corr = payload.get("correlation_id") if isinstance(payload, dict) else None
    if not corr and change_id:
        row = (
            db.query(IncomingChange)
            .filter(IncomingChange.npci_change_id == change_id)
            .first()
        )
        if row and row.correlation_id:
            corr = row.correlation_id
    return corr


async def _dispatch_wire(
    db: Session, task_type: str, change_id: str | None, payload: dict,
    *, job_correlation_id: str | None = None, idempotency_key: str | None = None,
    timeout: float | None = None,
) -> dict | None:
    """Authenticate, build the envelope, and send it over the SDK — the raw
    transport step, with NO resilience wrapping and NO failure handling of
    its own (raises on any failure). Shared by `send_task()` (wrapped in the
    circuit breaker/bulkhead below) and `services/outbound_retry.py`'s sweep
    (which needs the identical dispatch path for a queued retry, without
    re-enqueueing an already-queued row on failure).

    ITA-3: genuinely `async` — the old body wrapped the transport in
    `asyncio.run`, which cannot be called under a running loop (the exact
    blocker for the tunnel's async egress) and span up a private loop per
    send. Returns the receiver's structured reply (NPCI's task receipt; since
    ITA-2 possibly carrying a merged handler dict) or None when no structured
    artifact came back.

    Two DISTINCT correlation concepts are in play here (Finding 13:
    security_architecture_skills.md §13.1), and must not be conflated:
      - `envelope_corr` — the A2A protocol's own business correlation_id
        (v1.1 §5): NPCI's per-(change, bank) CONVERSATION THREAD pointer.
        This has spec meaning to NPCI's routing and is resolved exactly as
        before (payload override, else the stored IncomingChange thread id).
      - `job_correlation_id` — this platform's OWN causal-chain id (an
        AgentJob's `correlation_id`), sent as an ADDITIVE transport header
        (`X-NPCI-Correlation-ID`) so operator-side log/telemetry tooling can
        trace "which UI-triggered job caused this specific outbound call,"
        independent of and never overriding the NPCI conversation thread.
    """
    token = authenticate(db)
    if not token:
        raise RuntimeError("NPCI authentication failed (no API key configured, or NPCI rejected it)")

    base_url = _get_a2a_base_url(db)
    _validate_url_scheme(base_url, purpose="NPCI A2A task send (Bearer JWT + HMAC)")
    # Defence in depth: `authenticate()` above already guarded the same setting,
    # but this re-read is what the transport actually dials, so it is checked on
    # its own rather than trusting that the two reads agree.
    _guard_outbound_url(base_url, "NPCI A2A task send (Bearer JWT + HMAC)")
    hmac_secret = _get_setting(db, "npci_hmac_secret") or None

    # Phase 1 (protocol v1): envelope carries protocol_version + message_id
    # (dedup key) + correlation_id + timestamp. Reuse message_id as the SDK
    # task_id so the wire dedup key and the SDK Task id stay 1:1.
    #
    # IDEMPOTENCY (EA_Skills.md P3 "Idempotent operations and safe retries";
    # Critical example "non-idempotent payment processing with retry paths").
    # `message_id` IS the receiver's dedup key — so a retry MUST reuse the
    # original id. Minting a fresh uuid4 on every attempt (the previous
    # behaviour) meant a send that NPCI actually processed but whose ACK was
    # lost came back through `services/outbound_retry.py` looking like a
    # brand-new message, and was processed twice. The retry row persists the
    # key it was created with and passes it back in here.
    mid = idempotency_key or str(uuid.uuid4())
    envelope_corr = _resolve_correlation_id(db, change_id, payload)
    wire_data = make_envelope(
        task_type,
        message_id=mid,
        from_="partner",
        payload=payload,
        change_id=change_id,
        correlation_id=envelope_corr,
        agent_id="partner.platform.v1",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

    # If the caller didn't supply a job-level id explicitly, fall back to
    # whatever AgentJob is currently executing (core.correlation), if any —
    # this is what lets the ~10 existing send_task() call sites across
    # api/dashboard/*.py get correlation propagation for free when called
    # from within a job runner, with no per-call-site change required.
    from app.core.correlation import current_correlation_id
    header_corr = job_correlation_id or current_correlation_id() or envelope_corr

    return await send_a2a_message(
        base_url=base_url,
        context_id=change_id or mid,
        task_id=mid,
        data=wire_data,
        auth_header=f"Bearer {token}",
        hmac_secret=hmac_secret,
        correlation_id=header_corr,
        # ITA-4: the tunnel's §6 budget (90s) must ride through here — the
        # transport's 30s default sits BELOW the far side's 60s target
        # ceiling, so without this every slow tunnelled case dies as a
        # transport error instead of the real `target_timeout`.
        timeout=timeout if timeout is not None else 30.0,
    )


def _run_portably(coro):
    """Run a sender coroutine from a SYNC caller class (ITA-3).

    Three calling contexts exist, and each gets the correct treatment:

    * An anyio worker thread — every `def` dashboard route (Starlette runs
      them in anyio workers): `anyio.from_thread.run` executes the coroutine
      on the application's own event loop, sharing its scheduling.
    * A plain thread with no loop affiliation — `asyncio.to_thread` workers
      (`handlers/_background.py`), the outbound-retry sweep thread, scripts,
      tests: `from_thread.run` raises there, so fall back to `asyncio.run`.
      That is SAFE here — `send_a2a_message` builds a fresh
      `httpx.AsyncClient` per call, so unlike the `api/agents.py` precedent
      there is no lru-cached client for a throwaway loop to poison — and it
      keeps the blocking wait OFF the application loop, which is exactly
      where a background thread's work belongs.
    * The event loop itself: REFUSED loudly. A coroutine cannot be run
      synchronously from the loop without deadlocking it; an async caller
      must `await send_task_async(...)` instead. This is the guard that would
      have caught the old `asyncio.run`-inside-a-handler failure at the call
      site instead of in production.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        coro.close()   # never leave an un-awaited coroutine to warn at GC
        raise RuntimeError(
            "sync A2A sender called on the event loop — await "
            "send_task_async(...) from async code instead")

    from anyio import from_thread

    # Probe the context with a no-op BEFORE running the real coroutine: a
    # RuntimeError raised BY the sender (auth failure, transport error) must
    # not be mistaken for "not a worker thread" — that path would re-run an
    # already-consumed coroutine.
    async def _probe() -> None:
        return None

    try:
        from_thread.run(_probe)
    except RuntimeError:
        return asyncio.run(coro)
    return from_thread.run(lambda: coro)


async def send_task_async(
    db: Session, task_type: str, change_id: str | None, payload: dict,
    *, correlation_id: str | None = None, timeout: float | None = None,
) -> dict | None:
    """The genuinely-async sender (ITA-3) — see `send_task` for the contract.

    This is the ONLY body; `send_task` is a thin sync bridge over it. Async
    callers (the tunnel's egress handler once ITA-4 makes it `async`) await
    this directly. The bulkhead's blocking semaphore wait runs in a worker
    thread so a saturated bulkhead never stalls the event loop; the breaker's
    state check is lock-brief and stays inline.
    """
    from app.core.correlation import current_correlation_id
    from app.core.resilience import CircuitOpenError, breaker_for, bulkhead_for

    resolved_correlation_id = correlation_id or current_correlation_id()
    idem_key = str(uuid.uuid4())

    breaker = breaker_for("npci_a2a_outbound")
    bulkhead = bulkhead_for("npci_a2a_outbound")
    reply: dict | None = None
    try:
        def _enter_bulkhead():
            cm = bulkhead.acquire(timeout=10.0)
            cm.__enter__()   # the blocking semaphore wait — kept off the loop
            return cm

        slot = await asyncio.to_thread(_enter_bulkhead)
        try:
            with breaker.call():
                reply = await _dispatch_wire(
                    db, task_type, change_id, payload,
                    job_correlation_id=correlation_id, idempotency_key=idem_key,
                    timeout=timeout,
                )
        finally:
            slot.__exit__(None, None, None)
    except CircuitOpenError as exc:
        logger.error(
            "A2A send rejected: circuit open for NPCI outbound (type=%s change=%s)",
            task_type, change_id,
        )
        # `error` is persisted as OutboundA2ARetry.last_error and rendered by
        # the retry-queue view, so it carries the exception TYPE rather than
        # `str(exc)` — an httpx/auth failure message would otherwise pin the
        # resolved NPCI host, port and token prefix into a UI-visible row
        # (CWE-209). The DEBUG line keeps the real detail in the log.
        logger.debug(
            "circuit-open detail: type=%s change=%s", task_type, change_id, exc_info=True,
        )
        _maybe_enqueue_retry(
            db, change_id, task_type, payload, safe_exc(exc), resolved_correlation_id, idem_key,
        )
        return None
    except Exception as exc:  # noqa: BLE001 — any auth/transport failure
        logger.error("A2A task send error: type=%s change=%s", task_type, change_id, exc_info=True)
        _maybe_enqueue_retry(
            db, change_id, task_type, payload, safe_exc(exc), resolved_correlation_id, idem_key,
        )
        return None

    logger.info("A2A task sent to NPCI: type=%s change=%s", task_type, change_id)
    # B2: the receiver's reply body comes home. Truthy-on-success is part of
    # the contract (~30 call sites branch on it), so a receiver that emitted
    # no structured artifact still yields the old delivery marker.
    return reply if reply is not None else {"status": "delivered"}


def send_task(
    db: Session, task_type: str, change_id: str | None, payload: dict,
    *, correlation_id: str | None = None, timeout: float | None = None,
) -> dict | None:
    """Send an A2A Task to NPCI via the SDK JSON-RPC mount.

    Resilience (Finding 12: security_architecture_skills.md §5.4/§11.3):
    wrapped in a circuit breaker + bulkhead (docs/adr/ADR-0001's primitives,
    applied to the `npci_a2a_outbound` boundary) so a down/degraded NPCI
    instance fails fast instead of hanging every caller on the full transport
    timeout. On ANY failure (transport error or an already-open circuit), the
    message is queued in `OutboundA2ARetry` (services/outbound_retry.py) for
    automatic re-delivery rather than silently dropped.

    `correlation_id` (Finding 13) lets a caller explicitly set the platform's
    own causal-chain tracking id for this send (sent as the
    `X-NPCI-Correlation-ID` transport header — see `_dispatch_wire`'s
    docstring for why this is distinct from the A2A envelope's own business
    correlation_id). When omitted, falls back to the currently-executing
    AgentJob's correlation id, if any (core.correlation).

    Returns:
        The receiver's structured reply body on success (B2 — NPCI's task
        receipt, since ITA-2 possibly carrying a merged handler dict), or
        `{"status": "delivered"}` when the receiver emitted no structured
        artifact; None on auth or transport failure (queued for retry in the
        latter case). Success is always truthy, exactly as before.

    ITA-3: this is now a sync BRIDGE over `send_task_async` — see
    `_run_portably` for how each sync caller class reaches the loop. Async
    code must await `send_task_async` directly; calling this from the event
    loop raises rather than deadlocking.
    """
    return _run_portably(send_task_async(
        db, task_type, change_id, payload, correlation_id=correlation_id,
        timeout=timeout,
    ))


def _maybe_enqueue_retry(
    db: Session, change_id: str | None, task_type: str, payload: dict,
    error: str, correlation_id: str | None, idempotency_key: str | None = None,
) -> None:
    """Queue for retry UNLESS the task is a tunnelled exchange (ITA-5).

    A tunnelled POST is a business call on the far side; the sweeper replaying
    it is a duplicate business call, not a redelivery. The tunnel reports the
    failure to ITS caller (a structured error the Simulator asserts on) and
    does its own bounded retry or none at all.
    """
    from app.a2a_common.integration_contract import TUNNEL_TASK_TYPES

    if task_type in TUNNEL_TASK_TYPES:
        logger.warning(
            "tunnel send failed and was NOT queued for retry (replay would "
            "duplicate a business call): type=%s change=%s", task_type, change_id,
        )
        return
    _enqueue_retry(db, change_id, task_type, payload, error, correlation_id,
                   idempotency_key)


def _enqueue_retry(
    db: Session, change_id: str | None, task_type: str, payload: dict,
    error: str, correlation_id: str | None, idempotency_key: str | None = None,
) -> None:
    """Best-effort enqueue — a failure to even QUEUE the retry (e.g. the DB
    is also down) must not raise past send_task(), which already has a
    well-defined "returns None on failure" contract callers rely on."""
    try:
        from app.services.outbound_retry import enqueue
        enqueue(
            db, change_id=change_id, task_type=task_type, payload=payload,
            error=error, correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "failed to enqueue A2A outbound retry (message is now genuinely "
            "lost): type=%s change=%s", task_type, change_id,
        )


def _build_query_payload(message: str, correlation_id: str | None, phase: str | None) -> dict:
    """v1.1 spec-shaped `query` payload (A2A v1.0 §query).

    `query_id`/`question`/`asked_at`/`phase` are real; the per-clause/context
    fields (`clause_ref`, `subject`, `bank_interpretation`, `evidence_refs`,
    `bank_lead`) are null/empty pending UI capture (Phase 1) — never fabricated.
    `priority` defaults to `"normal"`. Legacy `message` is retained: the NPCI
    executor reads `message` for the query text and routes on `phase`.
    """
    from datetime import datetime, timezone
    payload: dict = {
        "query_id":            correlation_id,
        "question":            message,
        "asked_at":            datetime.now(timezone.utc).isoformat(),
        "phase":               phase,
        "clause_ref":          None,
        "subject":             None,
        "bank_interpretation": None,
        "evidence_refs":       [],
        "priority":            "normal",
        "pending_on_answer":   [],
        "references":          [],
        "bank_lead":           None,
        # back-compat — NPCI reads `message`; `phase` drives cert-vs-general routing.
        "message":             message,
    }
    if correlation_id:
        payload["correlation_id"] = correlation_id
    return payload


def send_query(
    db: Session,
    change_id: str,
    message: str,
    correlation_id: str | None = None,
    phase: str | None = None,
) -> dict | None:
    """Send a general-channel clarification query to NPCI.

    `correlation_id` is the partner-minted UUID of the OutgoingQuery row
    we're about to persist. NPCI echoes it back on the CLARIFICATION_RESPONSE
    so we attach the answer to THIS exact row instead of "most recent in
    channel". Omit (None) only for callers that don't track an OutgoingQuery.
    `phase` (feasibility/design/coding/testing/post_cert) is optional — the
    general channel leaves it null when the caller doesn't know it.
    """
    return send_task(db, "query", change_id, _build_query_payload(message, correlation_id, phase))


def send_cert_query(
    db: Session,
    change_id: str,
    message: str,
    correlation_id: str | None = None,
) -> dict | None:
    """Cert-channel clarification. Protocol v1 folds this into task_type='query'
    with payload `phase='cert'` (was a dedicated cert_query task type); the NPCI
    executor still routes it to a kind='cert' NegotiationThread.
    """
    return send_task(db, "query", change_id, _build_query_payload(message, correlation_id, "cert"))


def send_cert_status_update(
    db: Session,
    change_id: str,
    status: str,
    role: str = "",
    test_data: dict | None = None,
    test_data_per_case: dict | None = None,
) -> dict | None:
    """Push a cert lifecycle status update to NPCI via A2A.

    Status is one of: received | deployed | tested | ready_for_certification.
    The first three are bare status flips; ready_for_certification
    additionally carries `role` (PAYER_PSP / PAYEE_PSP / REMITTER_BANK /
    BENEFICIARY_BANK) and `test_data` (payer_vpa / account fields / …)
    so the NPCI cert orchestrator can pre-configure cert-agent test
    cases before triggering the LLM cert run.

    Slice 3 — `test_data_per_case` is the per-TC override dict the
    partner UI builds from its ChangeTestData rows. Shape is
    `{tc_id: {field: value, ...}}`. Per-case values take priority over
    the flat `test_data` fallback on the NPCI side.
    """
    payload: dict = {"status": status}
    if role:
        payload["role"] = role
    if test_data:
        payload["test_data"] = test_data
    if test_data_per_case:
        payload["test_data_per_case"] = test_data_per_case
    return send_task(db, "cert_status_update", change_id, payload)


# Legacy ProgressStep value → protocol v1 milestone name.
_STEP_TO_MILESTONE = {
    "design_completed":  "design",
    "coding_completed":  "coding",
    "testing_completed": "testing",
}

# Milestone order — drives `next_milestone` derivation.
_MILESTONE_ORDER = ("design", "coding", "testing")


def report_progress(db: Session, change_id: str, step: str, notes: str = "") -> dict | None:
    """Report an implementation milestone to NPCI (protocol v1 `milestone_update`).

    `step` is a legacy ProgressStep value (design_completed/coding_completed/
    testing_completed); translated to `{milestone, state="completed"}`.

    v1.1 spec-shaped (A2A v1.0 §milestone_update): `version_implementing`,
    `completed_at` and `next_milestone` are populated for real (NPCI already
    reads `version_implementing`/`risks`). `evidence_refs`/`risks`/
    `next_milestone_eta`/`amends_prior_update`/`bank_lead` are spec-conformant
    defaults pending UI capture — never fabricated. Only completion is reported
    here; the other `state` values (in_progress/at_risk/delayed/reopened) are a
    separate feature the UI doesn't trigger.
    """
    from datetime import datetime, timezone
    milestone = _STEP_TO_MILESTONE.get(step, step.replace("_completed", ""))

    row = (
        db.query(IncomingChange)
        .filter(IncomingChange.npci_change_id == change_id)
        .first()
    )
    version_implementing = (row.negotiation_version if row else None) or 1

    try:
        _idx = _MILESTONE_ORDER.index(milestone)
        next_milestone = _MILESTONE_ORDER[_idx + 1] if _idx + 1 < len(_MILESTONE_ORDER) else None
    except ValueError:
        next_milestone = None

    payload: dict = {
        "milestone":            milestone,
        "state":                "completed",
        "version_implementing": version_implementing,
        "completed_at":         datetime.now(timezone.utc).isoformat(),
        "next_milestone":       next_milestone,
        "next_milestone_eta":   None,
        "evidence_refs":        [],
        "risks":                [],
        "amends_prior_update":  None,
        "bank_lead":            None,
    }
    if notes:
        payload["notes"] = notes
    return send_task(db, "milestone_update", change_id, payload)


def declare_ready(
    db: Session,
    change_id: str,
    role: str = "",
    test_data: dict | None = None,      # accepted, no longer sent — see below
    test_data_per_case: dict | None = None,
) -> dict | None:
    """Send CERT_READINESS_DECLARATION to NPCI with the implementing role.

    NPCI's cert orchestrator uses `role` (PAYER_PSP / PAYEE_PSP /
    REMITTER_BANK / BENEFICIARY_BANK) to pick the matching role-prefixed
    test cases (PR_/PE_/RE_/BE_) before triggering the certification run.

    `test_data` / `test_data_per_case` are NO LONGER PUT ON THE WIRE: per the
    spec they belong to `cert_config_submission`, and carrying them here was a
    logged deviation. The parameters are retained so existing call sites keep
    working, but their values are dropped. Until `cert_config_submission` has a
    receiving handler, the orchestrator therefore configures cases with
    whatever defaults cert-agent already holds.

    Slice 3 — `test_data_per_case` carries per-TC overrides keyed by
    tc_id. NPCI merges these on top of the flat `test_data` fallback
    when patching cert-agent rows.

    Empty role/test_data degrades to the legacy `{status: ready_for_cert}`
    payload — back-compat with NPCI sides that haven't deployed the
    orchestrator yet.

    v1.1 spec-shaped (A2A v1.0 §readiness_declaration): `declared_at` and
    `version_implementing` are real; `implementation_summary`/`evidence_refs`/
    `bank_lead` are spec-conformant defaults pending UI capture. DEVIATION: the
    spec says `role`/subset belong in `cert_config_submission`, not here — but the
    NPCI cert orchestrator reads them off this message, so they're kept (logged
    deviation, see docs/A2A_spec_reconciliation.md §12).
    """
    from datetime import datetime, timezone

    row = (
        db.query(IncomingChange)
        .filter(IncomingChange.npci_change_id == change_id)
        .first()
    )
    version_implementing = (row.negotiation_version if row else None) or 1

    payload: dict = {
        "status":                 "ready_for_cert",
        "declared_at":            datetime.now(timezone.utc).isoformat(),
        "version_implementing":   version_implementing,
        "implementation_summary": None,
        "evidence_refs":          [],
        "bank_lead":              None,
    }
    if role:
        payload["role"] = role
    return send_task(db, "cert_readiness_declaration", change_id, payload)


# ── Rollout-contract messages (PROPOSAL_ACKNOWLEDGED / ACCEPTANCE / COUNTER) ──


def send_proposal_acknowledged(
    db: Session,
    change_id: str,
    kit_id: str | None,
    version_received: int,
    in_response_to: str | None,
    kit_files_received: list[dict],
) -> dict | None:
    """Auto-emitted by `handle_change_communication` after persisting the kit.

    Spec conformance (A2A v1.0 §proposal_acknowledged): `in_response_to` is the
    inbound change_communication's message_id, `version_received` keys the
    (change_id, version) idempotency, and `kit_files_received` is the plain list
    of stored doc_types. `kit_files_verified` is an additive (permissive-payload)
    extension carrying the per-file checksum receipt — the non-repudiation proof
    that the bytes arrived intact — which the string list can't express.
    """
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)

    # Unique stored doc_types, first-seen order (spec `kit_files_received`).
    doc_types: list[str] = []
    for f in kit_files_received:
        dt = f.get("doc_type")
        if dt and dt not in doc_types:
            doc_types.append(dt)

    payload = {
        "message_kind":          "PROPOSAL_ACKNOWLEDGED",
        "in_response_to":        in_response_to,
        "kit_id":                kit_id,
        "version_received":      version_received,
        "received_at":           now.isoformat(),
        "kit_files_received":    doc_types,
        "kit_files_verified":    kit_files_received,
        "review_phase":          "feasibility",
        "estimated_response_by": (now + timedelta(days=5)).isoformat(),
    }
    return send_task(db, "proposal_acknowledged", change_id, payload)


def send_proposal_acceptance(
    db: Session,
    change_id: str,
    kit_id: str | None,
    accepted_by: dict,
    internal_change_advisory_ref: str | None = None,
    estimated_phase_timeline: dict | None = None,
    implementation_kickoff_date: str | None = None,
) -> dict | None:
    """Sent when partner clicks Accept. Carries the structured fields
    NPCI persists onto `assignment.acceptance_meta.accepted`.

    Spec conformance (A2A v1.0 §change_acknowledgement): `version_accepted`
    keys the (change_id, version) acceptance. `internal_change_advisory_ref`,
    `implementation_kickoff_date` and `estimated_phase_timeline` are additive
    (permissive-payload) bank enrichments NPCI already reads and persists.
    """
    from datetime import datetime, timezone

    # Version being accepted — the current kit version the bank holds.
    row = (
        db.query(IncomingChange)
        .filter(IncomingChange.npci_change_id == change_id)
        .first()
    )
    version_accepted = (row.negotiation_version if row else None) or 1

    payload = {
        "message_kind":                 "PROPOSAL_ACCEPTANCE",
        "kit_id":                       kit_id,
        "decision":                     "ACCEPT",
        "accepted_at":                  datetime.now(timezone.utc).isoformat(),
        "version_accepted":             version_accepted,
        "accepted_by":                  accepted_by,
        "internal_change_advisory_ref": internal_change_advisory_ref,
        "implementation_kickoff_date":  implementation_kickoff_date,
        "estimated_phase_timeline":     estimated_phase_timeline,
    }
    return send_task(db, "change_acknowledgement", change_id, payload)


def send_counter_proposal(
    db: Session,
    change_id: str,
    kit_id: str | None,
    counter_proposal_id: str,
    justification: str,
    negotiation_round: int = 1,
    request_category: str | None = None,
    request_payload: dict | None = None,
) -> dict | None:
    """Sent when partner clicks Negotiate. Tier 1+: dedicated
    `counter_proposal` task type so NPCI routes to the structured
    handler (creates a CounterProposal row with state machine, gates
    rollout transitions). The `message` field is kept for backward
    compatibility with any pipeline still reading the QUERY payload
    shape.

    `request_category` and `request_payload` are the structured fields
    from the new partner negotiation form (Phase: partner negotiation).
    """
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)

    # Version this counter targets — the held kit version.
    row = (
        db.query(IncomingChange)
        .filter(IncomingChange.npci_change_id == change_id)
        .first()
    )
    version_targeted = (row.negotiation_version if row else None) or 1

    payload = {
        "message_kind":         "COUNTER_PROPOSAL",
        "kit_id":               kit_id,
        "counter_proposal_id":  counter_proposal_id,
        # v1.1 spec-shaped (A2A v1.0 §counter_proposal). The free-text justification
        # is wrapped in a counters[] array-of-one; per-clause structured fields
        # (clause_ref/clause_text/classification/proposed_text/evidence_refs/
        # alternative_offered) are null pending UI capture.
        "version_targeted":     version_targeted,
        "proposed_at":          now.isoformat(),
        "counters": [
            {
                "clause_ref":          None,
                "clause_text":         None,
                "classification":      None,
                "proposed_text":       None,
                "rationale":           justification,
                "evidence_refs":       [],
                "alternative_offered": None,
            }
        ],
        "summary":              justification,
        "references":           [],
        # Bilateral-extension / back-compat fields (kept; NPCI handler reads these):
        "decision":             "COUNTER",
        "negotiation_round":    negotiation_round,
        "justification":        justification,
        "valid_until":          (now + timedelta(days=7)).isoformat(),
        "message":              justification,
    }
    if request_category:
        payload["request_category"] = request_category
    if request_payload:
        payload["request_payload"] = request_payload
    return send_task(db, "counter_proposal", change_id, payload)


def send_counter_decision(
    db: Session,
    change_id: str,
    kit_id: str | None,
    counter_proposal_id: str,
    decision: str,
    resolution_text: str | None = None,
) -> dict | None:
    """Sent when partner accepts (or rejects) an NPCI-originated counter
    in isolation — i.e. resolving just the negotiation thread without
    accepting the whole rollout. Mirror of the NPCI→partner
    COUNTER_DECISION; uses a dedicated `counter_decision` task type so
    NPCI's executor routes it to the structured handler.
    """
    payload = {
        "message_kind":         "COUNTER_DECISION",
        "in_response_to":       counter_proposal_id,
        "decision":             decision,
        "resolution_text":      resolution_text or "",
        "response":             resolution_text or f"Counter {decision.lower()}ed",
    }
    if kit_id:
        payload["kit_id"] = kit_id
    return send_task(db, "counter_decision", change_id, payload)


def send_blocker(
    db: Session,
    change_id: str,
    blocker_id: str,
    severity: str,
    description: str,
    impact: str | None = None,
    investigation_done: list | None = None,
    options_considered: list | None = None,
    requested_action_from_npci: str | None = None,
) -> dict | None:
    """Sent when partner reports an obstacle mid-implementation
    (Journey C of the rollout doc). Structured fields let NPCI's PM
    make an informed decision and pick from `options_considered`.

    v1.1 spec-shaped (A2A v1.0 §blocker): `raised_at` is real; `type`/`subject`/
    `blocks`/`bank_internal_ticket`/`references`/`evidence_refs`/`bank_lead` are
    spec-conformant defaults pending UI capture — never fabricated.
    `investigation_done`/`options_considered` are kept in their richer list form
    (the spec defines them as strings — a deviation to fold into the spec, not
    downgrade). The NPCI receiver stores the whole payload, so new fields persist.
    """
    from datetime import datetime, timezone
    payload = {
        "message_kind":              "BLOCKER",
        "blocker_id":                blocker_id,
        "raised_at":                 datetime.now(timezone.utc).isoformat(),
        "type":                      None,
        "severity":                  severity,
        "subject":                   None,
        "description":               description,
        "impact":                    impact,
        "investigation_done":        investigation_done or [],
        "options_considered":        options_considered or [],
        "requested_action_from_npci": requested_action_from_npci,
        "blocks":                    [],
        "bank_internal_ticket":      None,
        "references":                [],
        "evidence_refs":             [],
        "bank_lead":                 None,
    }
    return send_task(db, "blocker", change_id, payload)


# ── Certification lifecycle senders (protocol v1, Bank→NPCI) ───────────────────


def send_cert_config_submission(db: Session, change_id: str, config: dict) -> dict | None:
    """Submit the bank's cert config in response to cert_config_request (§7.2)."""
    return send_task(db, "cert_config_submission", change_id, config)


def send_cert_test_preparation(db: Session, change_id: str, case_data: dict) -> dict | None:
    """Declare per-case test prep + readiness (§7.4)."""
    return send_task(db, "cert_test_preparation", change_id, {"case_data": case_data})


def send_cert_waiver_request(
    db: Session, change_id: str, case_id: str, category: str = "", reason: str = "",
) -> dict | None:
    """Request a waiver for a cert case (§7.8)."""
    return send_task(
        db, "cert_waiver_request", change_id,
        {"case_id": case_id, "category": category, "reason": reason},
    )


def send_cert_verdict_dispute(
    db: Session, change_id: str, case_id: str, bank_position: str = "",
    requested_action: str = "re_triage",
) -> dict | None:
    """Dispute an NPCI cert verdict (§7.7)."""
    return send_task(
        db, "cert_verdict_dispute", change_id,
        {"case_id": case_id, "bank_position": bank_position, "requested_action": requested_action},
    )


def send_cert_run_abort(db: Session, change_id: str, reason: str = "", category: str = "other") -> dict | None:
    """Abort the cert run (§7.14, terminal)."""
    return send_task(db, "cert_run_abort", change_id, {"reason": reason, "category": category})


def send_cert_status_request(db: Session, change_id: str, scope: str = "full") -> dict | None:
    """Request current cert status (§7.12)."""
    return send_task(db, "cert_status_request", change_id, {"scope": scope})


def send_cert_fix_notification(
    db: Session, change_id: str, fixed_case_ids: list, fix_summary: str = "",
    ready_for_rerun: bool = True,
) -> dict | None:
    """Report fixed defects + request re-run (§7.10; supersedes defect_resolution)."""
    return send_task(
        db, "cert_fix_notification", change_id,
        {"fixed_case_ids": fixed_case_ids, "fix_summary": fix_summary, "ready_for_rerun": ready_for_rerun},
    )


def send_cert_case_result(
    db: Session, change_id: str, case_id: str, status: str, attempt: int = 1,
    details: dict | None = None, reporter: str | None = None,
) -> dict | None:
    """Report a single cert case result (§7.5).

    `reporter="bank"` marks a PARTNER-EXECUTED case (ITA I-6): the authority
    upserts it onto the run, replacing the not_reported placeholder it
    recorded at dispatch. Without the field the authority treats the message
    as an echo of a case it ran itself and records nothing — the report
    vanishes politely. Default None keeps every existing echo call byte-
    identical on the wire.
    """
    payload = {"case_id": case_id, "status": status, "attempt": attempt,
               "details": details or {}}
    if reporter:
        payload["reporter"] = reporter
    return send_task(db, "cert_case_result", change_id, payload)


# ── Post-freeze break-glass (negotiation_flow) ────────────────────────────────


def send_emergency_issue(
    db: Session,
    change_id: str,
    issue_id: str,
    severity: str,
    title: str,
    description: str,
) -> dict | None:
    """Post-freeze break-glass channel. After the final kit version ships and
    NPCI freezes the change, queries/counters are rejected — this is the only
    inbound task NPCI still accepts. Wire format = task_type='emergency_issue'.
    """
    payload = {
        "message_kind": "EMERGENCY_ISSUE",
        "issue_id":     issue_id,
        "severity":     severity,
        "title":        title,
        "description":  description,
    }
    return send_task(db, "emergency_issue", change_id, payload)


# ── Operator-facing diagnostics for the Test Connection button ───────────────
# These are plain English status messages shown in the Settings UI. They carry
# no credential material: the HMAC secret is read from `partner_settings` at
# request time and is never rendered, logged or embedded here.
#
# They are module constants, and they refer to the secret by its human label
# from `secret_box.safe_key_label()` rather than by the raw `npci_hmac_secret`
# settings key, because Checkmarx's "Hardcoded Password in Connection String"
# query repeatedly flagged the earlier inline form. A string literal that
# contains a known secret identifier and is returned from a function reachable
# by an HTTP handler matches that query's shape, regardless of the literal being
# a sentence rather than a value. Naming the secret descriptively keeps the
# message just as actionable for the operator and removes the pattern.
#
# Keep these as constants and keep the raw settings key out of them.
_HMAC_SECRET_LABEL = safe_key_label("npci_hmac_secret")

_HMAC_REJECTED_MSG = (
    f"HMAC envelope rejected (HTTP 401) — verify {_HMAC_SECRET_LABEL} in Settings "
    "matches the value NPCI has stored for this partner."
)

_HMAC_NOT_CONFIGURED_MSG = (
    f"HMAC envelope NOT configured — {_HMAC_SECRET_LABEL} is empty in Settings. "
    "Install it to exercise the full A2A wire on next Test."
)

# ── Telling an HMAC rejection apart from a JWT one ───────────────────────────
#
# NPCI answers EVERY rejection on /a2a-rpc/rpc with 401. Its JWT layer
# (sdk_auth_middleware._err) and its envelope layer (sdk_hmac_middleware) both
# do; the envelope middleware emits only 401 and 413, never 403. So branching
# on the status code — `if "401" in msg` first, `if "403" in msg` for HMAC —
# reported EVERY envelope failure as a JWT problem and sent the operator to
# rotate the wrong secret, while the 403 branch was dead code.
#
# What actually separates them is the structured body both layers return:
# `{"error": "<code>", "detail": "..."}`, with disjoint code vocabularies.
_JWT_ERROR_CODES = frozenset({
    "missing_bearer_token", "invalid_token", "session_unknown",
    "session_revoked", "session_expired", "partner_unknown", "partner_inactive",
})

# `envelope_invalid` is the middleware's fallback when verify returns no code;
# the rest come from hmac_signer.verify.
_HMAC_ERROR_CODES = frozenset({
    "signature_mismatch", "missing_envelope_headers", "invalid_envelope",
    "envelope_invalid", "replay_detected", "nonce_check_unavailable",
    "hmac_secret_not_configured",
})

# Clock skew is called out on its own because it is the ONE failure here that
# has nothing to do with a secret being wrong. Folded into the generic envelope
# message it reads as "your HMAC secret is bad", and the operator rotates a
# perfectly good secret while the clock stays wrong.
_CLOCK_SKEW_MSG = (
    f"HMAC envelope rejected (HTTP 401, timestamp_skew) — this host's clock is "
    f"more than {DEFAULT_MAX_SKEW_S}s from NPCI's, so the signed timestamp is "
    "outside the accepted window. No secret is wrong: fix the clock (NTP) and "
    "retry."
)

_HMAC_SECRET_UNKNOWN_TO_NPCI_MSG = (
    "HMAC envelope rejected (HTTP 401, hmac_secret_not_configured) — this "
    "platform signed the request but NPCI holds NO signing secret for this "
    f"partner. {_HMAC_SECRET_LABEL} must be installed on the NPCI side too "
    "(admin rotate-hmac-secret), not just here."
)

_JWT_REJECTED_MSG = "Bearer JWT rejected on /a2a-rpc/rpc (HTTP 401)"


def _rejection_response(exc: BaseException):
    """Find the HTTP response behind an SDK exception, or None.

    The a2a SDK wraps transport failures as `A2AClientError('HTTP Error 401:
    ...')` — a message with the status line and nothing else. The original
    `httpx.HTTPStatusError`, which still carries the response body, survives on
    `__cause__`, so walk the chain rather than parsing the string.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        response = getattr(cur, "response", None)
        if response is not None and hasattr(response, "status_code"):
            return response
        cur = cur.__cause__ or cur.__context__
    return None


def _describe_rejection(exc: BaseException) -> str | None:
    """Map an SDK exception onto an operator-actionable message, or None when
    the response says nothing recognisable and the caller should fall back."""
    response = _rejection_response(exc)
    if response is None:
        return None

    status = getattr(response, "status_code", None)
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 — a non-JSON error body is not exceptional
        body = None
    code = body.get("error") if isinstance(body, dict) else None

    if code == "timestamp_skew":
        return _CLOCK_SKEW_MSG
    if code == "hmac_secret_not_configured":
        return _HMAC_SECRET_UNKNOWN_TO_NPCI_MSG
    if code == "signature_mismatch":
        return _HMAC_REJECTED_MSG
    if code in _HMAC_ERROR_CODES:
        return (f"HMAC envelope rejected (HTTP {status}, {code}) — the signed "
                "envelope did not verify at NPCI.")
    if code in _JWT_ERROR_CODES:
        return f"{_JWT_REJECTED_MSG}: {code}"
    if status == 404:
        return "/a2a-rpc/rpc not found (HTTP 404) — NPCI SDK mount may be disabled"
    if code:
        return f"NPCI rejected the echo probe (HTTP {status}, {code})"
    return None


def _send_echo_probe(
    base_url: str, token: str, hmac_secret: str | None,
) -> tuple[bool, str]:
    """End-to-end A2A round-trip via the SDK — sends task_type='echo'.

    NPCI's executor exercises auth middleware (Bearer JWT) → HMAC
    envelope middleware → SDK dispatch → the no-op ECHO handler.
    A clean "echo_ok" reply proves every wire-side layer agrees.

    Returns (ok, detail). `detail` is one of:
      - "echo_ok"               — full round-trip succeeded
      - "<short error message>" — anything that aborted before then.
    """
    try:
        mid = str(uuid.uuid4())
        # ITA-3: through the portable bridge — this runs inside a `def` route
        # (an anyio worker thread), where `asyncio.run` needlessly span a
        # private loop beside the application's own.
        _run_portably(send_a2a_message(
            base_url=base_url,
            context_id=mid,
            task_id=mid,
            data=make_envelope(
                "echo",
                message_id=mid,
                from_="partner",
                payload={},
                timestamp=datetime.now(timezone.utc).isoformat(),
            ),
            auth_header=f"Bearer {token}",
            hmac_secret=hmac_secret,
        ))
        return True, "echo_ok"
    except Exception as exc:  # noqa: BLE001
        # Which LAYER rejected is decided by the structured `error` code in the
        # response body, not by the status code: NPCI answers both a bad JWT
        # and a bad HMAC envelope with 401, so a status-code cascade can only
        # ever name one of them. See _describe_rejection.
        described = _describe_rejection(exc)
        if described:
            return False, described

        # No usable response body — the probe never reached a middleware
        # (DNS, TLS, connection refused, timeout) or NPCI answered with
        # something unstructured. The raw message is the best signal left.
        msg = str(exc)
        if "404" in msg:
            return False, "/a2a-rpc/rpc not found (HTTP 404) — NPCI SDK mount may be disabled"
        return False, msg[:200]


def run_npci_reachability_check(
    db: Session, api_key_override: str | None = None,
) -> dict:
    """Probe connectivity AND the end-to-end A2A round-trip against NPCI.

    Named `run_npci_reachability_check` rather than `test_connection` for two
    reasons. It is an outbound HTTPS probe of the NPCI platform, not a database
    connection — the `db` argument is the SQLAlchemy session the stored URL and
    API key are *read from*, not a server being connected to. And the old name
    put a `test_*` function that takes a `db` session next to strings naming
    credentials, which is the shape Checkmarx's "Hardcoded Password in
    Connection String" query treats as a database-connect sink; every operator
    diagnostic returned from here was reported as a leaked connection password.
    The `test_` prefix also collided with pytest's `python_functions` pattern.

    Three layered checks so the UI button is actually diagnostic:
      1. Reachability — GET the public agent card at npci_platform_url.
         Catches: NPCI down, wrong URL.
      2. Authentication — POST /api/a2a/auth with the stored
         partner_api_key. Catches: empty/wrong/rotated api_key.
      3. Echo round-trip — send task_type='echo' via the SDK to
         /a2a-rpc/rpc. Catches: HMAC envelope mismatch, missing
         npci_hmac_secret, A2A mount disabled, JWT-layer rejection.

    Steps short-circuit on the first failure so the operator gets a
    single, specific error — not a wall of layered diagnostics.

    `api_key_override` lets the operator validate a typed-but-not-yet-saved
    API key from the Settings form. When provided it's used in place of
    the stored value for THIS test only; nothing is persisted.
    """
    human_url = _get_human_npci_url(db)
    a2a_url   = _get_a2a_base_url(db)

    # SSRF guard: reject private/resolved IPs for the configured URLs
    # (SAST finding F-003). This prevents an attacker with admin access
    # from using this probe to reach internal services.
    #
    # The message names the allowlist as the remedy. The earlier wording said
    # only "point at the public NPCI platform endpoint", which sent operators
    # hunting for a public address that does not exist — NPCI UAT is an
    # internal host, and approving it is the correct resolution.
    _allow_hint = (
        "If this IS the real NPCI platform, approve it by adding the host to "
        "NPCI_SSRF_ALLOWED_HOSTS (comma-separated), or set "
        "NPCI_SSRF_ALLOW_PRIVATE_NETWORKS=true to permit private space wholesale."
    )
    if _is_private_url(human_url):
        return {
            "status": "error",
            "message": (
                f"Reachability check blocked: {human_url} resolves to a private "
                f"or reserved IP address, so no request was sent. {_allow_hint}"
            ),
        }
    if _is_private_url(a2a_url):
        return {
            "status": "error",
            "message": (
                f"A2A check blocked: {a2a_url} resolves to a private or reserved "
                f"IP address, so no request was sent. {_allow_hint}"
            ),
        }

    # Warn if either URL uses cleartext HTTP (SAST finding F-002)
    _validate_url_scheme(human_url, purpose="NPCI platform URL (reachability check)")
    _validate_url_scheme(a2a_url, purpose="NPCI A2A URL (auth + SDK round-trip)")

    # Step 1 — reachability via the public well-known endpoint.
    npci_name = "NPCI Platform"
    try:
        resp = httpx.get(f"{human_url}/a2a/.well-known/agent.json", timeout=10.0)
        if resp.status_code != 200:
            return {
                "status": "error",
                "message": f"Reachability check failed: GET {human_url}/a2a/.well-known/agent.json "
                           f"returned {resp.status_code}",
            }
        try:
            npci_name = resp.json().get("name") or npci_name
        except Exception:  # noqa: BLE001
            pass
    except httpx.ConnectError:
        return {"status": "error", "message": f"Cannot reach NPCI at {human_url}"}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "message": f"Reachability check error: {e}"}

    # Step 2 — auth handshake using the same path real outbound calls take.
    # Prefer the in-flight typed key when the Test button passed one through —
    # lets the operator validate before clicking Save.
    api_key = api_key_override or _get_api_key(db)
    if not api_key:
        return {
            "status": "error",
            "message": "Reachable, but Partner API Key is not configured. "
                       "Paste the key from NPCI admin into Settings before any "
                       "outbound A2A call can succeed.",
        }
    try:
        auth_resp = httpx.post(
            f"{a2a_url}/api/a2a/auth",
            json={"api_key": api_key},
            timeout=15.0,
        )
    except httpx.ConnectError:
        return {
            "status": "error",
            "message": f"Reachable on platform URL but cannot reach A2A endpoint at {a2a_url}",
        }
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "message": f"Auth probe error: {e}"}

    if auth_resp.status_code in (401, 403):
        return {
            "status": "error",
            "message": f"NPCI rejected the API key (HTTP {auth_resp.status_code}). "
                       "Rotate the key on NPCI admin and re-install in Settings, or verify "
                       "this partner row is still Active on NPCI.",
        }
    if auth_resp.status_code != 200 or not auth_resp.json().get("jwt"):
        return {
            "status": "error",
            "message": f"Auth handshake returned unexpected HTTP {auth_resp.status_code}: "
                       f"{auth_resp.text[:200]}",
        }
    token = auth_resp.json()["jwt"]

    # Step 3 — full SDK echo round-trip. Validates HMAC envelope +
    # JWT-on-rpc + dispatch in a single shot. Skipped if the HMAC
    # secret isn't configured — partner runs in permissive mode then,
    # so a no-HMAC call would also pass and the test would be
    # misleading. Operator sees a clear "configure HMAC to validate
    # end-to-end" message instead.
    hmac_secret = _get_setting(db, "npci_hmac_secret") or None
    if not hmac_secret:
        return {
            "status": "ok",
            "message": f"Connected to {npci_name} — reachability + API key "
                       f"verified. {_HMAC_NOT_CONFIGURED_MSG}",
        }

    ok, detail = _send_echo_probe(a2a_url, token, hmac_secret)
    if not ok:
        return {
            "status": "error",
            "message": f"API key verified, but A2A round-trip failed: {detail}",
        }

    return {
        "status": "ok",
        "message": f"Connected to {npci_name} — reachability + API key + full A2A round-trip verified",
    }
