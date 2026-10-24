"""A2A wire protocol v1 — the single source of truth for the message contract.

Implements `A2A_PROTOCOL_DESIGN.pdf` (v1.0): 28 typed messages + `echo`, one
common envelope, 22 structured error codes, and per-message metadata
(direction / lifecycle phase / correlation key). The PDF is *descriptive*; this
module is *authoritative* — the drift test (`tests/a2a_common/test_protocol_contract.py`)
asserts the frozen 28 here match the PDF appendix exactly.

MIRROR: this file is byte-identical with
`partner-platform/backend/app/a2a_common/protocol.py`. Change one, change both.

Deltas from the frozen PDF (see `docs/A2A_PROTOCOL_MIGRATION.md` for rationale):
  1. envelope carries `protocol_version`
  2. envelope carries `message_id` — the uniform dedup/idempotency key
  3. `echo` uses the full envelope like every other message
  4. `change_id` lives in the envelope only (not duplicated into payloads)

Phase 0 establishes the enum, error codes, envelope, and per-message metadata.
Concrete per-message payload models are filled in as each message-group phase
lands (Phases 2–4); until then a payload validates against the permissive
`PayloadBase` (extra fields allowed, matching the "preserve extensions" decision).

Pure module: stdlib + pydantic only. No SDK, no app models, no DB — so it
imports cleanly in both the NPCI app graph and the partner SQLite harness.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

# Bump on any breaking envelope/contract change. Delta #1.
PROTOCOL_VERSION = "1.0"

# The cert-agent is not a distinct wire identity — it signs as the NPCI
# platform. Outbound cert-agent messages set from=SENDER_NPCI and
# agent_id=CERT_ORCHESTRATOR_AGENT_ID (PDF §3 caution).
SENDER_NPCI = "npci-platform"
CERT_ORCHESTRATOR_AGENT_ID = "npci.cert_orchestrator.v1"


class A2ATaskType(str, enum.Enum):
    """Every message type on the unified wire.

    The first 28 members are the frozen PDF contract (Parts A/B) plus the
    `echo` utility. The trailing `CERT_WITNESS_*` members are documented
    v1.0+ext extensions — tracked separately by `is_ext()` so the drift
    test does not compare them against the frozen appendix.
    """

    # --- Part A: Phase C lifecycle (keyed on change_id) ---
    CHANGE_COMMUNICATION    = "change_communication"
    PROPOSAL_ACKNOWLEDGED   = "proposal_acknowledged"
    CHANGE_ACKNOWLEDGEMENT  = "change_acknowledgement"
    QUERY                   = "query"
    CLARIFICATION_RESPONSE  = "clarification_response"
    COUNTER_PROPOSAL        = "counter_proposal"
    COUNTER_DECISION        = "counter_decision"
    MILESTONE_UPDATE        = "milestone_update"
    MILESTONE_STATUS_REQUEST = "milestone_status_request"
    MILESTONE_STATUS_REPORT = "milestone_status_report"
    CERT_READINESS_DECLARATION = "cert_readiness_declaration"
    BLOCKER                 = "blocker"
    BLOCKER_STATUS_UPDATE   = "blocker_status_update"
    BLOCKER_RESOLUTION      = "blocker_resolution"

    # --- Part B: Certification lifecycle (keyed on cflow_id + cert_attempt) ---
    CERT_CONFIG_REQUEST       = "cert_config_request"
    CERT_CONFIG_SUBMISSION    = "cert_config_submission"
    CERT_SETUP_NOTIFICATION   = "cert_setup_notification"
    CERT_TEST_PREPARATION     = "cert_test_preparation"
    CERT_CASE_RESULT          = "cert_case_result"
    CERT_VERDICT_NOTIFICATION = "cert_verdict_notification"
    CERT_VERDICT_DISPUTE      = "cert_verdict_dispute"
    CERT_WAIVER_REQUEST       = "cert_waiver_request"
    CERT_WAIVER_DECISION      = "cert_waiver_decision"
    CERT_FIX_NOTIFICATION     = "cert_fix_notification"
    CERT_SIGNOFF_NOTIFICATION = "cert_signoff_notification"
    CERT_STATUS_REQUEST       = "cert_status_request"
    CERT_STATUS_REPORT        = "cert_status_report"
    CERT_RUN_ABORT            = "cert_run_abort"

    # --- Part C: Utility ---
    ECHO = "echo"

    # --- v1.0+ext (not in the frozen 28) ---
    CERT_WITNESS_REQUEST   = "cert_witness_request"
    CERT_WITNESS_SCHEDULED = "cert_witness_scheduled"
    # NPCI→bank advisory: a kit revision is in progress (round closed, new
    # version being prepared). Partners hold queries until it ships.
    REVISION_IN_PROGRESS   = "revision_in_progress"
    # NPCI→bank per-partner round lifecycle notices. Round-state lives only on
    # the NPCI side (`negotiation_round_states`); these give the partner a
    # first-class signal of "you're in round N, deadline T" and "round N is
    # closed because X" so it stays in sync when a round transitions without a
    # coinciding kit dispatch (PM force-advance, silent-acceptance sweep).
    ROUND_OPENED           = "round_opened"
    ROUND_CLOSED           = "round_closed"
    # Certification transactions over A2A (docs/upi_over_a2a_plan.md): the
    # Egress/Ingress wrappers relay the engines' signed XML between switches as
    # opaque base64. Direction is not fixed per type — it derives from
    # (task_type, payload.initiated_by) via txn_direction(); a mismatch with
    # the authenticated caller is refused as WRONG_DIRECTION.
    TXN_REQUEST            = "txn_request"
    TXN_RESPONSE           = "txn_response"
    # Consolidated results pull: the bank asks for the outcomes of the batch it
    # declared ready; NPCI answers with ONE cert_case_result-shaped body
    # (results[] + pending[]). Additive — the per-case push stays.
    CERT_VAL_REQUEST   = "cert_val_request"
    # 2026-10-12 — bank-driven completion + signoff pair. The bank uses these
    # when it believes it has finished the certification; NPCI's handlers
    # compile a per-case report and (for signoff) fire cert_signoff_notification
    # when every applicable case is complete (pass or waiver-granted). The
    # report is sent on both the completion-check path AND the signoff path, so
    # a bank that skips straight to signoff still gets the ledger. See
    # `backend/app/services/cert_completion_report.py` + the two handlers in
    # `backend/app/a2a_common/npci_handlers.py`.
    CERT_COMPLETION_CHECK_REQUEST = "cert_completion_check_request"
    CERT_COMPLETION_REPORT        = "cert_completion_report"
    CERT_SIGNOFF_REQUEST          = "cert_signoff_request"
    # 2026-10-12 — dispute flow. The bank disputes a verdict with
    # `cert_verdict_dispute` (already in Part B above); NPCI's dispute
    # analyser agent decides whether the verdict stands and replies with
    # `cert_verdict_dispute_response`. After reading the justification the
    # bank either disputes again or accepts via `cert_verdict_acceptance`;
    # the subsequent `cert_fix_notification` carries the list of cases
    # the bank has fixed and asks NPCI to re-execute (NPCI-initiated) or
    # signals the bank will re-fire (bank-initiated).
    CERT_VERDICT_DISPUTE_RESPONSE = "cert_verdict_dispute_response"
    CERT_VERDICT_ACCEPTANCE       = "cert_verdict_acceptance"
    # v1.2 — NPCI's consolidated results (renamed from cert_case_result).
    CERT_TEST_RESPONSE            = "cert_test_response"

    def is_ext(self) -> bool:
        return self in _EXT_TASK_TYPES


_EXT_TASK_TYPES = frozenset(
    {
        A2ATaskType.CERT_WITNESS_REQUEST,
        A2ATaskType.CERT_WITNESS_SCHEDULED,
        A2ATaskType.REVISION_IN_PROGRESS,
        A2ATaskType.ROUND_OPENED,
        A2ATaskType.ROUND_CLOSED,
        A2ATaskType.TXN_REQUEST,
        A2ATaskType.TXN_RESPONSE,
        A2ATaskType.CERT_VAL_REQUEST,
        A2ATaskType.CERT_COMPLETION_CHECK_REQUEST,
        A2ATaskType.CERT_COMPLETION_REPORT,
        A2ATaskType.CERT_SIGNOFF_REQUEST,
        A2ATaskType.CERT_VERDICT_DISPUTE_RESPONSE,
        A2ATaskType.CERT_VERDICT_ACCEPTANCE,
        A2ATaskType.CERT_TEST_RESPONSE,
    }
)


class Direction(str, enum.Enum):
    NPCI_TO_BANK = "npci_to_bank"
    BANK_TO_NPCI = "bank_to_npci"
    EITHER       = "either"


class CorrelationKey(str, enum.Enum):
    """Which top-level id threads a message's lifecycle (PDF §5)."""

    CHANGE_ID = "change_id"
    CFLOW_ID  = "cflow_id"
    NONE      = "none"


class ErrorCode(str, enum.Enum):
    """The 22 structured rejection codes (PDF §10). Returned in the A2A
    response so banks' SOC can alert on the string without log parsing."""

    # HMAC envelope
    SIGNATURE_MISMATCH       = "signature_mismatch"
    TIMESTAMP_SKEW           = "timestamp_skew"
    REPLAY_DETECTED          = "replay_detected"
    MISSING_ENVELOPE_HEADERS = "missing_envelope_headers"
    # JWT
    INVALID_TOKEN   = "invalid_token"
    SESSION_REVOKED = "session_revoked"
    SESSION_EXPIRED = "session_expired"
    # Authorisation
    PARTNER_INACTIVE          = "partner_inactive"
    MTLS_REQUIRED             = "mtls_required"
    MTLS_FINGERPRINT_MISMATCH = "mtls_fingerprint_mismatch"
    # Network
    IP_NOT_ALLOWED = "ip_not_allowed"
    # Correlation
    PARTNER_MISMATCH = "partner_mismatch"
    UNKNOWN_ID       = "unknown_id"
    # State machine
    INVALID_STATE_TRANSITION = "invalid_state_transition"
    # Agent identity (optional, PDF §4 Options B/C)
    UNKNOWN_AGENT                 = "unknown_agent"
    AGENT_NOT_AUTHORIZED_FOR_TASK = "agent_not_authorized_for_task"
    # Protocol / schema
    UNKNOWN_TASK_TYPE       = "unknown_task_type"
    PAYLOAD_VALIDATION_ERROR = "payload_validation_error"
    # Validation
    BANK_IDENTITY_MISMATCH  = "bank_identity_mismatch"
    CERT_FINGERPRINT_MISMATCH = "cert_fingerprint_mismatch"
    BANK_UNREACHABLE        = "bank_unreachable"
    # 2026-10-12 — cert_config_submission field-level validation (Phase V1).
    # Emitted by backend/app/services/cert_config_validation.py when the
    # bank's submission fails a format / consistency / cross-ref / business
    # rule. BANK_IDENTITY_MISMATCH above now carries the (psp_org_id vs
    # target_identity.org_id) semantics the orchestrator used to spell
    # inline — one canonical name across handler + orchestrator paths.
    INVALID_CONFIG_FIELD    = "invalid_config_field"
    CONFIG_INCONSISTENT     = "config_inconsistent"
    UNKNOWN_SUBSET          = "unknown_subset"
    BANK_ID_MISMATCH        = "bank_id_mismatch"
    # Handler
    EXECUTOR_ERROR = "executor_error"

    @property
    def layer(self) -> str:
        return _ERROR_LAYERS[self]


_ERROR_LAYERS: dict[ErrorCode, str] = {
    ErrorCode.SIGNATURE_MISMATCH: "hmac",
    ErrorCode.TIMESTAMP_SKEW: "hmac",
    ErrorCode.REPLAY_DETECTED: "hmac",
    ErrorCode.MISSING_ENVELOPE_HEADERS: "hmac",
    ErrorCode.INVALID_TOKEN: "jwt",
    ErrorCode.SESSION_REVOKED: "jwt",
    ErrorCode.SESSION_EXPIRED: "jwt",
    ErrorCode.PARTNER_INACTIVE: "authz",
    ErrorCode.MTLS_REQUIRED: "authz",
    ErrorCode.MTLS_FINGERPRINT_MISMATCH: "authz",
    ErrorCode.IP_NOT_ALLOWED: "network",
    ErrorCode.PARTNER_MISMATCH: "correlation",
    ErrorCode.UNKNOWN_ID: "correlation",
    ErrorCode.INVALID_STATE_TRANSITION: "state_machine",
    ErrorCode.UNKNOWN_AGENT: "agent",
    ErrorCode.AGENT_NOT_AUTHORIZED_FOR_TASK: "agent",
    ErrorCode.UNKNOWN_TASK_TYPE: "protocol",
    ErrorCode.PAYLOAD_VALIDATION_ERROR: "schema",
    ErrorCode.BANK_IDENTITY_MISMATCH: "validation",
    ErrorCode.CERT_FINGERPRINT_MISMATCH: "validation",
    ErrorCode.BANK_UNREACHABLE: "validation",
    ErrorCode.INVALID_CONFIG_FIELD: "validation",
    ErrorCode.CONFIG_INCONSISTENT: "validation",
    ErrorCode.UNKNOWN_SUBSET: "validation",
    ErrorCode.BANK_ID_MISMATCH: "validation",
    ErrorCode.EXECUTOR_ERROR: "handler",
}


# The live auth/HMAC middleware emit a few finer-grained operational codes than
# the canonical 22 (extra diagnostic detail for ops). They are NOT rewritten —
# they're catalogued here with their canonical mapping so the full emitted set
# is auditable in one place and SOC rules can normalise to the doc-22.
MIDDLEWARE_ERROR_CODES: dict[str, ErrorCode] = {
    "missing_bearer_token": ErrorCode.INVALID_TOKEN,
    "missing_envelope_headers": ErrorCode.MISSING_ENVELOPE_HEADERS,
    "session_unknown":      ErrorCode.INVALID_TOKEN,
    "partner_unknown":      ErrorCode.PARTNER_INACTIVE,
    "mtls_not_provisioned": ErrorCode.MTLS_REQUIRED,
}


@dataclass(frozen=True)
class MessageSpec:
    """Static contract metadata for one task type (PDF §6.x / §7.x summaries)."""

    task_type: A2ATaskType
    direction: Direction
    correlation_key: CorrelationKey
    cardinality: str
    ext: bool = False


def _spec(tt, d, ck, card, ext=False) -> MessageSpec:
    return MessageSpec(tt, d, ck, card, ext)


_T = A2ATaskType
_D = Direction
_C = CorrelationKey

# Source of truth for direction + correlation per message. Built from the PDF's
# Part A / Part B summary tables and the §5 identifiers table.
MESSAGES: dict[A2ATaskType, MessageSpec] = {
    # Part A
    _T.CHANGE_COMMUNICATION:     _spec(_T.CHANGE_COMMUNICATION, _D.NPCI_TO_BANK, _C.CHANGE_ID, "once per (change_id, version)"),
    _T.PROPOSAL_ACKNOWLEDGED:    _spec(_T.PROPOSAL_ACKNOWLEDGED, _D.BANK_TO_NPCI, _C.CHANGE_ID, "once per (change_id, version_received)"),
    _T.CHANGE_ACKNOWLEDGEMENT:   _spec(_T.CHANGE_ACKNOWLEDGEMENT, _D.BANK_TO_NPCI, _C.CHANGE_ID, "once per (change_id, version_accepted)"),
    _T.QUERY:                    _spec(_T.QUERY, _D.BANK_TO_NPCI, _C.CHANGE_ID, "any; per question"),
    _T.CLARIFICATION_RESPONSE:   _spec(_T.CLARIFICATION_RESPONSE, _D.NPCI_TO_BANK, _C.CHANGE_ID, "one per query_id"),
    _T.COUNTER_PROPOSAL:         _spec(_T.COUNTER_PROPOSAL, _D.BANK_TO_NPCI, _C.CHANGE_ID, "any; per proposal"),
    _T.COUNTER_DECISION:         _spec(_T.COUNTER_DECISION, _D.NPCI_TO_BANK, _C.CHANGE_ID, "optional; per counter_proposal_id"),
    _T.MILESTONE_UPDATE:         _spec(_T.MILESTONE_UPDATE, _D.BANK_TO_NPCI, _C.CHANGE_ID, "per milestone transition"),
    _T.MILESTONE_STATUS_REQUEST: _spec(_T.MILESTONE_STATUS_REQUEST, _D.EITHER, _C.CHANGE_ID, "any time"),
    _T.MILESTONE_STATUS_REPORT:  _spec(_T.MILESTONE_STATUS_REPORT, _D.EITHER, _C.CHANGE_ID, "reply to request"),
    _T.CERT_READINESS_DECLARATION: _spec(_T.CERT_READINESS_DECLARATION, _D.BANK_TO_NPCI, _C.CHANGE_ID, "once per (change_id, version_implementing)"),
    _T.BLOCKER:                  _spec(_T.BLOCKER, _D.BANK_TO_NPCI, _C.CHANGE_ID, "per impediment"),
    _T.BLOCKER_STATUS_UPDATE:    _spec(_T.BLOCKER_STATUS_UPDATE, _D.NPCI_TO_BANK, _C.CHANGE_ID, "any per blocker_id"),
    _T.BLOCKER_RESOLUTION:       _spec(_T.BLOCKER_RESOLUTION, _D.NPCI_TO_BANK, _C.CHANGE_ID, "once per blocker_id (terminal)"),
    # Part B
    _T.CERT_CONFIG_REQUEST:      _spec(_T.CERT_CONFIG_REQUEST, _D.NPCI_TO_BANK, _C.CFLOW_ID, "once per cflow_id"),
    _T.CERT_CONFIG_SUBMISSION:   _spec(_T.CERT_CONFIG_SUBMISSION, _D.BANK_TO_NPCI, _C.CFLOW_ID, "once per cflow_id"),
    _T.CERT_SETUP_NOTIFICATION:  _spec(_T.CERT_SETUP_NOTIFICATION, _D.NPCI_TO_BANK, _C.CFLOW_ID, "once per cert_attempt"),
    _T.CERT_TEST_PREPARATION:    _spec(_T.CERT_TEST_PREPARATION, _D.BANK_TO_NPCI, _C.CFLOW_ID, "incremental"),
    _T.CERT_CASE_RESULT:         _spec(_T.CERT_CASE_RESULT, _D.EITHER, _C.CFLOW_ID, "per (case_id, attempt)"),
    _T.CERT_VERDICT_NOTIFICATION: _spec(_T.CERT_VERDICT_NOTIFICATION, _D.NPCI_TO_BANK, _C.CFLOW_ID, "per failed case"),
    _T.CERT_VERDICT_DISPUTE:     _spec(_T.CERT_VERDICT_DISPUTE, _D.BANK_TO_NPCI, _C.CFLOW_ID, "per disputed verdict"),
    _T.CERT_WAIVER_REQUEST:      _spec(_T.CERT_WAIVER_REQUEST, _D.BANK_TO_NPCI, _C.CFLOW_ID, "per case"),
    _T.CERT_WAIVER_DECISION:     _spec(_T.CERT_WAIVER_DECISION, _D.NPCI_TO_BANK, _C.CFLOW_ID, "per waiver request"),
    _T.CERT_FIX_NOTIFICATION:    _spec(_T.CERT_FIX_NOTIFICATION, _D.BANK_TO_NPCI, _C.CFLOW_ID, "per fix batch"),
    _T.CERT_SIGNOFF_NOTIFICATION: _spec(_T.CERT_SIGNOFF_NOTIFICATION, _D.NPCI_TO_BANK, _C.CFLOW_ID, "once per cflow_id"),
    _T.CERT_STATUS_REQUEST:      _spec(_T.CERT_STATUS_REQUEST, _D.EITHER, _C.CFLOW_ID, "any time"),
    _T.CERT_STATUS_REPORT:       _spec(_T.CERT_STATUS_REPORT, _D.EITHER, _C.CFLOW_ID, "reply or push"),
    _T.CERT_RUN_ABORT:           _spec(_T.CERT_RUN_ABORT, _D.EITHER, _C.CFLOW_ID, "once; terminal"),
    # Part C
    _T.ECHO:                     _spec(_T.ECHO, _D.BANK_TO_NPCI, _C.NONE, "any time"),
    # ext
    _T.CERT_WITNESS_REQUEST:     _spec(_T.CERT_WITNESS_REQUEST, _D.BANK_TO_NPCI, _C.CFLOW_ID, "per witness session", ext=True),
    _T.CERT_WITNESS_SCHEDULED:   _spec(_T.CERT_WITNESS_SCHEDULED, _D.NPCI_TO_BANK, _C.CFLOW_ID, "per witness session", ext=True),
    _T.REVISION_IN_PROGRESS:     _spec(_T.REVISION_IN_PROGRESS, _D.NPCI_TO_BANK, _C.CHANGE_ID, "per revision round", ext=True),
    _T.ROUND_OPENED:             _spec(_T.ROUND_OPENED, _D.NPCI_TO_BANK, _C.CHANGE_ID, "per (change_id, partner_id, round_number)", ext=True),
    _T.ROUND_CLOSED:             _spec(_T.ROUND_CLOSED, _D.NPCI_TO_BANK, _C.CHANGE_ID, "per (change_id, partner_id, round_number)", ext=True),
    # EITHER here is the static summary; the binding rule per message is
    # txn_direction(task_type, initiated_by) below.
    _T.TXN_REQUEST:              _spec(_T.TXN_REQUEST, _D.EITHER, _C.CFLOW_ID, "per transaction", ext=True),
    _T.TXN_RESPONSE:             _spec(_T.TXN_RESPONSE, _D.EITHER, _C.CFLOW_ID, "one per txn_request", ext=True),
    _T.CERT_VAL_REQUEST:     _spec(_T.CERT_VAL_REQUEST, _D.BANK_TO_NPCI, _C.CFLOW_ID, "per batch", ext=True),
    # 2026-10-12 — completion + signoff pull pair. Both bank-initiated; the
    # NPCI-side report (direction NPCI_TO_BANK) is sent either as a sync reply
    # artifact to the completion-check request OR fired asynchronously before
    # signoff.
    _T.CERT_COMPLETION_CHECK_REQUEST: _spec(_T.CERT_COMPLETION_CHECK_REQUEST, _D.BANK_TO_NPCI, _C.CFLOW_ID, "any time",          ext=True),
    _T.CERT_COMPLETION_REPORT:        _spec(_T.CERT_COMPLETION_REPORT,        _D.NPCI_TO_BANK, _C.CFLOW_ID, "per report",        ext=True),
    _T.CERT_SIGNOFF_REQUEST:          _spec(_T.CERT_SIGNOFF_REQUEST,          _D.BANK_TO_NPCI, _C.CFLOW_ID, "once per cflow_id", ext=True),
    _T.CERT_VERDICT_DISPUTE_RESPONSE: _spec(_T.CERT_VERDICT_DISPUTE_RESPONSE, _D.NPCI_TO_BANK, _C.CFLOW_ID, "per dispute",       ext=True),
    _T.CERT_VERDICT_ACCEPTANCE:       _spec(_T.CERT_VERDICT_ACCEPTANCE,       _D.BANK_TO_NPCI, _C.CFLOW_ID, "per verdict",       ext=True),
    _T.CERT_TEST_RESPONSE:            _spec(_T.CERT_TEST_RESPONSE,            _D.NPCI_TO_BANK, _C.CFLOW_ID, "per batch + one final", ext=True),
}

# The 28 frozen messages + echo, in PDF order. Used by the drift test and by
# callers that need to enumerate the non-ext contract.
FROZEN_TASK_TYPES: tuple[A2ATaskType, ...] = tuple(
    tt for tt in MESSAGES if not MESSAGES[tt].ext
)


class PayloadBase(BaseModel):
    """Base for per-message payload models.

    `extra="allow"` honours the "preserve extensions" decision: payload bodies
    may carry fields beyond the PDF (e.g. multi-round counter-back, observation
    metadata) without failing validation. Concrete subclasses with required
    fields land per message-group phase.
    """

    model_config = ConfigDict(extra="allow")


# task_type → payload model. Phase 0 maps every message to PayloadBase; Phases
# 2–4 replace entries with concrete schemas as each group is built.
PAYLOAD_SCHEMAS: dict[A2ATaskType, type[PayloadBase]] = {
    tt: PayloadBase for tt in MESSAGES
}


# ── Txn over A2A (docs/upi_over_a2a_plan.md, Message contract) ─────────────────
#
# txn_request / txn_response carry certification transactions between the
# switch engines. The wrappers move opaque bytes: base64 of exactly what the
# sender's engine signed (the UPI XML is XML-DSig signed, so any re-encoding
# breaks the signature). Nothing here parses the XML. Never on the wire:
# expected response codes, expected ACK errors, verdict hints.


class TxnInitiator(str, enum.Enum):
    """Who fired the transaction — NOT who sends a given message (the
    response to a BANK-initiated request travels NPCI→bank)."""

    NPCI = "NPCI"
    BANK = "BANK"


# (task_type, initiated_by) → the only legal wire direction. Ingress derives
# the legal sender from this and compares it with the authenticated caller; a
# mismatch is refused as WRONG_DIRECTION.
TXN_DIRECTIONS: dict[tuple[A2ATaskType, TxnInitiator], Direction] = {
    (A2ATaskType.TXN_REQUEST,  TxnInitiator.NPCI): Direction.NPCI_TO_BANK,
    (A2ATaskType.TXN_REQUEST,  TxnInitiator.BANK): Direction.BANK_TO_NPCI,
    (A2ATaskType.TXN_RESPONSE, TxnInitiator.NPCI): Direction.BANK_TO_NPCI,
    (A2ATaskType.TXN_RESPONSE, TxnInitiator.BANK): Direction.NPCI_TO_BANK,
}


def txn_direction(task_type: A2ATaskType, initiated_by: TxnInitiator) -> Direction:
    return TXN_DIRECTIONS[(task_type, initiated_by)]


class TxnFaultCode(str, enum.Enum):
    """Wrapper-level delivery faults — infrastructure, never a certification
    FAIL. Toward the engine a timeout fault is delivered as no answer, so the
    engine applies its own timeout (UPI: U09); wrappers never fabricate a
    NACK."""

    SWITCH_UNREACHABLE = "SWITCH_UNREACHABLE"
    SWITCH_TIMEOUT     = "SWITCH_TIMEOUT"
    NO_ACTIVE_RUN      = "NO_ACTIVE_RUN"
    UNKNOWN_PARTNER    = "UNKNOWN_PARTNER"
    UNKNOWN_TARGET     = "UNKNOWN_TARGET"
    WRONG_DIRECTION    = "WRONG_DIRECTION"


class TxnHeader(PayloadBase):
    """Only the content type; everything else rides in metadata."""

    content_type: str


class TxnMetadata(PayloadBase):
    product: str
    content_encoding: str = "base64"
    txn_ref: str
    size_bytes: int
    # uri is never dialled as given: the receiver matches it against the
    # product allow-list and joins it to its own configured base, else
    # UNKNOWN_TARGET. Absent on an ACK.
    uri: str | None = None
    sent_at: str | None = None
    timeout_ms: int | None = None
    # txn_response — the switch timings the partner reports (its internal
    # legs are otherwise invisible to NPCI)
    received_at: str | None = None
    responded_at: str | None = None
    latency_ms: int | None = None
    # ACK only
    http_status: int | None = None
    acked_at: str | None = None


class TxnBody(PayloadBase):
    header: TxnHeader
    metadata: TxnMetadata
    txn_payload: str   # base64 of the raw bytes, verbatim


class TxnIterationRef(PayloadBase):
    attempt: int
    attempt_type: str | None = None


class TxnCertificationRef(PayloadBase):
    """The (product, org_id) identity + run context every transaction
    envelope names. Inbound, org_id must belong to the JWT's partner, else
    partner_mismatch."""

    product: str
    org_id: str
    run_id: str | None = None
    partner_id: str | None = None
    cert_name: str | None = None
    subset: str | None = None
    phase: str | None = None
    iteration: TxnIterationRef | None = None


class TxnTestCaseRef(PayloadBase):
    tc_id: str | None = None


class TxnMessagePayload(PayloadBase):
    """payload of txn_request / txn_response."""

    initiated_by: TxnInitiator
    certification: TxnCertificationRef
    txn: TxnBody
    test_case: TxnTestCaseRef | None = None


class TxnFaultBody(PayloadBase):
    fault_code: TxnFaultCode
    fault_detail: str | None = None


class TxnReply(PayloadBase):
    """The A2A reply artifact: the relayed ACK bytes, or a fault — exactly
    one of `txn` / `fault`. Wrappers never author an ACK; dedup on
    message_id replays the stored one."""

    acks_message_id: str
    kind: str = "txn_ack"
    correlation_id: str | None = None
    txn: TxnBody | None = None
    fault: TxnFaultBody | None = None


PAYLOAD_SCHEMAS[A2ATaskType.TXN_REQUEST] = TxnMessagePayload
PAYLOAD_SCHEMAS[A2ATaskType.TXN_RESPONSE] = TxnMessagePayload


class CertValRequestPayload(PayloadBase):
    """cert_val_request (bank -> NPCI). `batch_id` is the cflow_id of the
    run whose outcomes are wanted; `case_ids` narrows it (empty = whole batch)."""

    model_config = ConfigDict(extra="forbid")

    batch_id: str
    case_ids: list[str] | None = None


PAYLOAD_SCHEMAS[A2ATaskType.CERT_VAL_REQUEST] = CertValRequestPayload


class Envelope(BaseModel):
    """The common outer structure every message rides in (PDF §3 + deltas 1–4).

    `extra="forbid"`: the envelope is a strict contract. Extensions belong in
    `payload` (which is permissive), never as new envelope fields.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    protocol_version: str = Field(default=PROTOCOL_VERSION)  # delta #1
    message_id: str                                          # delta #2 (dedup key)
    task_type: A2ATaskType
    # `from` is a Python keyword — store as from_, (de)serialise as "from".
    from_: str = Field(alias="from")
    agent_id: str | None = None
    agent_run_id: str | None = None
    correlation_id: str | None = None
    timestamp: str | None = None
    change_id: str | None = None   # delta #4 — envelope only, not in payloads
    cflow_id: str | None = None
    cert_attempt: int | None = None
    payload: dict = Field(default_factory=dict)


def spec_for(task_type: A2ATaskType) -> MessageSpec:
    return MESSAGES[task_type]


def parse_task_type(value: str) -> A2ATaskType:
    """Map a wire string to the enum. Raises ValueError on unknown values —
    callers translate that into an `unknown_task_type` rejection."""
    return A2ATaskType(value)


# --- Envelope helpers (Phase 1 plumbing) ---------------------------------------
#
# These build/read the envelope as a plain dict and accept `task_type` as a
# raw string. During the migration the wire still carries pre-rename task_types
# (e.g. status_update) that aren't in A2ATaskType yet, so we must NOT validate
# membership here — that tightening lands with the renames (Phase 2) and strict
# `Envelope` validation (Phase 5). The strict `Envelope` model above remains the
# authoritative contract for the post-cutover state.


def make_envelope(
    task_type,
    *,
    message_id: str,
    from_: str,
    payload: dict | None = None,
    change_id: str | None = None,
    cflow_id: str | None = None,
    cert_attempt: int | None = None,
    correlation_id: str | None = None,
    agent_id: str | None = None,
    agent_run_id: str | None = None,
    timestamp: str | None = None,
) -> dict:
    """Assemble the wire envelope dict with the v1 fields (deltas 1–2 always set).

    `task_type` may be an enum (any str-enum) or a plain string. Optional ids are
    omitted when None so the wire stays clean; receivers treat absent optionals
    as null. `message_id` (dedup key) and `protocol_version` are always present.
    """
    tt = task_type.value if isinstance(task_type, enum.Enum) else str(task_type)
    env: dict = {
        "protocol_version": PROTOCOL_VERSION,
        "message_id": message_id,
        "task_type": tt,
        "from": from_,
        "payload": payload or {},
    }
    for key, val in (
        ("change_id", change_id),
        ("cflow_id", cflow_id),
        ("cert_attempt", cert_attempt),
        ("correlation_id", correlation_id),
        ("agent_id", agent_id),
        ("agent_run_id", agent_run_id),
        ("timestamp", timestamp),
    ):
        if val is not None:
            env[key] = val
    return env


@dataclass(frozen=True)
class InboundEnvelope:
    """Tolerant read of an inbound envelope. Every field optional so legacy
    (pre-v1) messages parse without error during the migration window."""

    task_type: str
    payload: dict
    from_: str | None
    message_id: str | None
    correlation_id: str | None
    change_id: str | None
    cflow_id: str | None
    cert_attempt: int | None
    agent_id: str | None
    agent_run_id: str | None
    timestamp: str | None
    protocol_version: str | None


def read_envelope(data: dict | None) -> InboundEnvelope:
    """Extract envelope fields from an inbound message dict, tolerantly."""
    data = data or {}
    return InboundEnvelope(
        task_type=data.get("task_type") or "",
        payload=data.get("payload") or {},
        from_=data.get("from"),
        message_id=data.get("message_id"),
        correlation_id=data.get("correlation_id"),
        change_id=data.get("change_id"),
        cflow_id=data.get("cflow_id"),
        cert_attempt=data.get("cert_attempt"),
        agent_id=data.get("agent_id"),
        agent_run_id=data.get("agent_run_id"),
        timestamp=data.get("timestamp"),
        protocol_version=data.get("protocol_version"),
    )
