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
import base64
import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timezone

import httpx
from a2a.client.errors import A2AClientError, AgentCardResolutionError
from sqlalchemy.orm import Session

from app.a2a_common.client import send_a2a_message
from app.a2a_common.protocol import make_envelope
from app.models import IncomingChange, PartnerSetting

logger = logging.getLogger(__name__)


def _get_setting(db: Session, key: str, default: str = "") -> str:
    row = db.get(PartnerSetting, key)
    return row.value if row and row.value else default


def _get_a2a_base_url(db: Session) -> str:
    """Direct service URL for partner→NPCI A2A traffic.

    Defaults to the docker service name. Override via
    `partner_settings.npci_a2a_url`. Distinct from `npci_platform_url`
    which is the human-facing URL used for `test_connection`; the SDK
    needs the bare host that backs `/a2a-rpc/rpc`, not a UI mount.
    """
    url = _get_setting(db, "npci_a2a_url", "http://npci_backend:8000").strip().rstrip("/")
    return url


def _get_human_npci_url(db: Session) -> str:
    """UI-facing NPCI URL (used by `test_connection`)."""
    url = _get_setting(db, "npci_platform_url", "http://localhost").strip().rstrip("/")
    if "://localhost" in url:
        url = url.replace("://localhost", "://host.docker.internal")
    return url


def _get_api_key(db: Session, bank_org_id: str | None = None) -> str:
    """Pick the api_key to authenticate as.

    A partner backend can hold identities for multiple banks in the same
    deployment (e.g. "Bank Sim" + "HDFC"). `partner_settings.partner_api_key_by_org`
    stores a JSON map {bank_org_id: api_key}; when the caller knows which
    bank it's speaking as, we use that key. Falls back to the legacy single
    `partner_api_key` when no map entry exists — preserves the old behaviour.
    """
    if bank_org_id:
        raw = _get_setting(db, "partner_api_key_by_org", "")
        if raw:
            try:
                mapping = json.loads(raw)
                if isinstance(mapping, dict) and mapping.get(bank_org_id):
                    return str(mapping[bank_org_id])
            except (ValueError, TypeError):
                logger.warning("partner_api_key_by_org is not valid JSON — falling back to global key")
    return _get_setting(db, "partner_api_key", "")


# JWT cache: (npci url, api_key) -> (monotonic expiry, token). NPCI's access
# token lives 15 minutes (spec §Tokens); a handshake per message cost three
# extra round-trips each. Refreshed `_TOKEN_SKEW_SECONDS` before it lapses.
_TOKEN_SKEW_SECONDS = 60.0
_DEFAULT_TOKEN_TTL_SECONDS = 600.0
_tokens: dict[tuple[str, str], tuple[float, str]] = {}
_token_lock = threading.Lock()


def _jwt_expiry(token: str) -> float | None:
    """`exp` from the token's claims, unverified — only used to time the cache."""
    try:
        seg = token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        return float(json.loads(base64.urlsafe_b64decode(seg))["exp"])
    except Exception:  # noqa: BLE001
        return None


def _cache_token(key: tuple[str, str], token: str) -> None:
    exp = _jwt_expiry(token)
    ttl = (exp - time.time() if exp else _DEFAULT_TOKEN_TTL_SECONDS) - _TOKEN_SKEW_SECONDS
    if ttl > 0:
        with _token_lock:
            _tokens[key] = (time.monotonic() + ttl, token)


def invalidate_token(db: Session, bank_org_id: str | None = None) -> None:
    """Forget the cached token — call when NPCI rejected it."""
    with _token_lock:
        _tokens.pop((_get_a2a_base_url(db), _get_api_key(db, bank_org_id=bank_org_id)), None)


def authenticate(db: Session, bank_org_id: str | None = None) -> str | None:
    """Authenticate with NPCI and return a JWT token (cached until near expiry).

    Hits /api/a2a/auth on the direct service URL — the auth endpoint
    is unprotected by HMAC/CIDR middleware, so going direct vs through
    nginx is equivalent and direct keeps the dev path simple.

    `bank_org_id` picks a per-bank api_key from
    `partner_settings.partner_api_key_by_org` when set — otherwise
    the global `partner_api_key` is used.
    """
    url = _get_a2a_base_url(db)
    api_key = _get_api_key(db, bank_org_id=bank_org_id)
    if not api_key:
        logger.error("NPCI API key not configured")
        return None

    key = (url, api_key)
    with _token_lock:
        hit = _tokens.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]

    try:
        resp = httpx.post(f"{url}/api/a2a/auth", json={"api_key": api_key}, timeout=15.0)
        if resp.status_code == 200:
            token = resp.json().get("jwt")
            logger.info("NPCI auth success")
            if token:
                _cache_token(key, token)
            return token
        else:
            logger.warning("NPCI auth failed: %d %s", resp.status_code, resp.text[:200])
            return None
    except Exception as e:
        logger.error("NPCI auth error", exc_info=True)
        return None


# Bank -> NPCI sends retry transient failures (spec: exponential backoff from
# 1 s, capped at 60 s, on 5xx and connection failures only). Every attempt
# reuses the same envelope message_id, so NPCI's dedup makes a resend harmless.
_SEND_ATTEMPTS = 4
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_CAP_SECONDS = 60.0


def _http_status(exc: BaseException) -> int | None:
    """The HTTP status behind a send failure, however the SDK wrapped it."""
    e: BaseException | None = exc
    while e is not None:
        if isinstance(e, httpx.HTTPStatusError):
            return e.response.status_code
        if isinstance(e, AgentCardResolutionError) and e.status_code:
            return e.status_code
        m = re.search(r"\b(?:HTTP|status(?: code)?:?)\s*(\d{3})\b", str(e), re.I)
        if m:
            return int(m.group(1))
        e = e.__cause__ or e.__context__
    return None


def _is_transient(exc: BaseException) -> bool:
    """Worth resending: no answer (connect/timeout) or a 5xx. Never a 4xx."""
    status = _http_status(exc)
    if status is not None:
        return status >= 500
    e: BaseException | None = exc
    while e is not None:
        if isinstance(e, httpx.TransportError):
            return True
        if type(e).__name__ == "A2AClientTimeoutError":
            return True
        if isinstance(e, A2AClientError) and "network communication error" in str(e).lower():
            return True
        e = e.__cause__ or e.__context__
    return False


def send_task(
    db: Session, task_type: str, change_id: str | None, payload: dict,
    bank_org_id: str | None = None, cflow_id: str | None = None,
) -> dict | None:
    """Send an A2A Task to NPCI via the SDK JSON-RPC mount.

    `bank_org_id`, when set, picks a per-bank api_key from
    `partner_settings.partner_api_key_by_org` so the outbound is
    attributed to that specific partner_agents row on NPCI's side.

    Returns:
        {"status": "delivered"} on success, None on auth or transport failure.
    """
    token = authenticate(db, bank_org_id=bank_org_id)
    if not token:
        return None

    base_url = _get_a2a_base_url(db)
    hmac_secret = _get_setting(db, "npci_hmac_secret") or None

    # Phase 1 (protocol v1): envelope carries protocol_version + message_id
    # (dedup key) + correlation_id + timestamp. Reuse message_id as the SDK
    # task_id so the wire dedup key and the SDK Task id stay 1:1.
    mid = str(uuid.uuid4())
    # correlation_id resolution (v1.1 §5): a caller-supplied payload value wins
    # (the query path uses it as the OutgoingQuery row pointer — see note below);
    # otherwise echo NPCI's per-(change, bank) thread id captured on receipt, so
    # every reply threads back to the right conversation.
    corr = payload.get("correlation_id") if isinstance(payload, dict) else None
    if not corr and change_id:
        row = (
            db.query(IncomingChange)
            .filter(IncomingChange.npci_change_id == change_id)
            .first()
        )
        if row and row.correlation_id:
            corr = row.correlation_id
    wire_data = make_envelope(
        task_type,
        message_id=mid,
        from_="partner",
        payload=payload,
        change_id=change_id,
        cflow_id=cflow_id or (payload.get("cflow_id") if isinstance(payload, dict) else None),
        correlation_id=corr,
        agent_id="partner.platform.v1",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

    attempt, reauthed = 0, False
    while True:
        attempt += 1
        try:
            asyncio.run(send_a2a_message(
                base_url=base_url,
                context_id=change_id or mid,
                task_id=mid,
                data=wire_data,
                auth_header=f"Bearer {token}",
                hmac_secret=hmac_secret,
            ))
            logger.info(
                "A2A task sent to NPCI: type=%s change=%s attempt=%d",
                task_type, change_id, attempt,
            )
            return {"status": "delivered"}
        except Exception as exc:
            if not reauthed and _http_status(exc) == 401:
                # The cached token may have been revoked or expired early: one
                # fresh handshake, then resend. A second 401 is a real rejection.
                reauthed = True
                invalidate_token(db, bank_org_id)
                token = authenticate(db, bank_org_id=bank_org_id)
                if token:
                    continue
            elif attempt < _SEND_ATTEMPTS and _is_transient(exc):
                delay = min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
                logger.warning(
                    "A2A task send failed (%s); retry %d/%d in %.0fs: type=%s change=%s",
                    exc, attempt, _SEND_ATTEMPTS - 1, delay, task_type, change_id,
                )
                time.sleep(delay)
                continue
            logger.error(
                "A2A task send error: type=%s change=%s attempts=%d",
                task_type, change_id, attempt, exc_info=True,
            )
            return None


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
    """Submit the bank's cert config in response to cert_config_request (§7.2).

    The bank identity we're authenticating as is the same one being submitted,
    so `bank_org_id` is picked up from `config` itself for the per-partner
    api_key lookup.
    """
    bank_org_id = str(config.get("bank_org_id") or "").strip() or None
    return send_task(db, "cert_config_submission", change_id, config, bank_org_id=bank_org_id)


def send_cert_test_preparation(
    db: Session, change_id: str, case_data: dict, bank_org_id: str | None = None,
) -> dict | None:
    """Declare per-case test prep + readiness (§7.4)."""
    return send_task(
        db, "cert_test_preparation", change_id, {"case_data": case_data},
        bank_org_id=bank_org_id,
    )


def send_cert_waiver_request(
    db: Session, change_id: str, case_id: str, category: str = "", reason: str = "",
    bank_org_id: str | None = None,
) -> dict | None:
    """Request a waiver for a cert case (§7.8).

    `bank_org_id` picks the per-bank api_key from
    `partner_settings.partner_api_key_by_org` so the outbound is attributed
    to the correct partner_agents row on NPCI's side.
    """
    return send_task(
        db, "cert_waiver_request", change_id,
        {"case_id": case_id, "category": category, "reason": reason},
        bank_org_id=bank_org_id,
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
    """Request current cert status (§7.12).

    Fire-and-forget: NPCI answers with an asynchronous `cert_status_report`
    push, so this cannot tell the caller anything about the current state.
    For "what does NPCI have right now?" use `fetch_npci_status` instead.
    """
    return send_task(db, "cert_status_request", change_id, {"scope": scope})


# ── Synchronous status pull (reconciliation) ─────────────────────────────────


def fetch_npci_status(db: Session, change_id: str) -> dict:
    """GET NPCI's authoritative stage/state for one change.

    Reads `/api/a2a/status/changes/{change_id}` with the same api_key→JWT
    handshake every A2A send uses — no new credential. Unlike `send_task`,
    this is a plain REST GET with a body we actually read, which is the whole
    point: A2A sends drain the response, so they can never answer "are we in
    sync?".

    Returns the snapshot dict. Raises RuntimeError with an operator-readable
    reason on auth/transport/HTTP failure — the caller surfaces it verbatim
    rather than showing a half-empty comparison the bank might trust.
    """
    token = authenticate(db)
    if not token:
        raise RuntimeError(
            "Could not authenticate with NPCI — check the partner API key in Settings."
        )
    url = f"{_get_a2a_base_url(db)}/api/a2a/status/changes/{change_id}"
    try:
        resp = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=20.0)
    except Exception as exc:
        logger.error("NPCI status fetch transport error: change=%s", change_id, exc_info=True)
        raise RuntimeError(f"Could not reach NPCI: {exc}") from exc

    if resp.status_code == 404:
        raise RuntimeError(
            "NPCI has no record of this change being assigned to us. "
            "This is itself a discrepancy — raise it with NPCI."
        )
    if resp.status_code != 200:
        logger.warning(
            "NPCI status fetch failed: change=%s http=%d body=%s",
            change_id, resp.status_code, resp.text[:200],
        )
        raise RuntimeError(f"NPCI returned HTTP {resp.status_code}")
    return resp.json()


def send_cert_fix_notification(
    db: Session, change_id: str, fixed_case_ids: list, fix_summary: str = "",
    ready_for_rerun: bool = True, case_data: dict | None = None,
) -> dict | None:
    """Report fixed defects + request re-run (§7.10; supersedes defect_resolution).

    `case_data` ({case_id: {amount, mpin, ...}}) carries the refreshed test
    inputs NPCI should retest with; omitted when the old ones still apply."""
    payload = {"fixed_case_ids": fixed_case_ids, "fix_summary": fix_summary, "ready_for_rerun": ready_for_rerun}
    if case_data:
        payload["case_data"] = case_data
    return send_task(db, "cert_fix_notification", change_id, payload)


def send_cert_verdict_acceptance(
    db: Session, change_id: str, case_id: str | None = None, *,
    case_ids: list[str] | None = None, note: str | None = None, cflow_id: str | None = None,
) -> dict | None:
    """Accept NPCI's verdict for one or more cases and commit to fixing them.

    One call covers every case: `case_ids` (or a single `case_id`)."""
    ids = list(case_ids or ([case_id] if case_id else []))
    payload: dict = {"case_ids": ids}
    if note:
        payload["note"] = note
    return send_task(db, "cert_verdict_acceptance", change_id, payload, cflow_id=cflow_id)


def send_cert_val_request(
    db: Session, change_id: str, batch_id: str, case_ids: list | None = None,
) -> dict | None:
    """Pull the outcomes of a batch (or of named cases) in one reply."""
    payload: dict = {"batch_id": batch_id}
    if case_ids:
        payload["case_ids"] = case_ids
    return send_task(db, "cert_val_request", change_id, payload)


def send_cert_completion_check_request(db: Session, change_id: str, cflow_id: str) -> dict | None:
    """Ask NPCI for the per-case completion ledger (answered by cert_completion_report)."""
    return send_task(db, "cert_completion_check_request", change_id, {"cflow_id": cflow_id})


def send_cert_signoff_request(db: Session, change_id: str, cflow_id: str) -> dict | None:
    """Ask for sign-off; NPCI sends the completion report and, if complete, the sign-off."""
    return send_task(db, "cert_signoff_request", change_id, {"cflow_id": cflow_id})


def send_cert_case_result(
    db: Session, change_id: str, case_id: str, status: str, attempt: int = 1,
    details: dict | None = None, bank_org_id: str | None = None,
) -> dict | None:
    """Report a single cert case result (§7.5).

    `bank_org_id` picks the per-bank api_key from
    `partner_settings.partner_api_key_by_org` so the outbound is attributed
    to the correct partner_agents row on NPCI's side.
    """
    return send_task(
        db, "cert_case_result", change_id,
        {"case_id": case_id, "status": status, "attempt": attempt, "details": details or {}},
        bank_org_id=bank_org_id,
    )


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
        asyncio.run(send_a2a_message(
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
        # The SDK raises httpx.HTTPStatusError for 4xx/5xx; surface the
        # status code so the operator knows which layer rejected.
        msg = str(exc)
        if "401" in msg:
            # NPCI's HMAC layer answers 401 too (signature_mismatch, missing_envelope_headers,
            # replay_detected), so a 401 cannot tell the two apart from here.
            return False, ("Bearer JWT or HMAC envelope rejected on /a2a-rpc/rpc (HTTP 401) — "
                           "check npci_jwt_secret and npci_hmac_secret match NPCI's stored values")
        if "403" in msg:
            return False, "Rejected on /a2a-rpc/rpc (HTTP 403) — the caller is not authorised (partner status, IP allow-list or mTLS)"
        if "404" in msg:
            return False, "/a2a-rpc/rpc not found (HTTP 404) — NPCI SDK mount may be disabled"
        return False, msg[:200]


def test_connection(db: Session, api_key_override: str | None = None) -> dict:
    """Test connectivity AND end-to-end A2A round-trip against NPCI.

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
            "message": f"Connected to {npci_name} — reachability + API key verified. "
                       "HMAC envelope NOT configured (npci_hmac_secret empty); install it "
                       "to exercise the full A2A wire on next Test.",
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
