# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Partner Platform SQLite models."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON, Boolean, DateTime, Integer, LargeBinary, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class IncomingChange(Base):
    __tablename__ = "incoming_changes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    npci_change_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # NPCI's per-(change, bank) A2A thread correlation_id, captured from the
    # inbound change_communication envelope. Echoed back on every reply about
    # this change (v1.1 §5) so NPCI threads our replies to the right conversation.
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    initial_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    enhanced_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="received")
    received_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    # Partner's decision on the rollout: pending | acknowledged | accepted | negotiating.
    # Drives the DecisionPanel UI; auto-ack flips pending → acknowledged.
    decision: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # Latest NPCI counter-proposal received (multi-round negotiation).
    # Stored as JSON: {counter_proposal_id, negotiation_round, justification,
    # valid_until, received_at, status:'open'|'responded'}. NULL until NPCI
    # sends their first counter; cleared back to NULL when partner responds.
    npci_counter: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Append-only audit log of every NPCI counter-proposal this partner
    # has received, with how it was resolved. Each entry is the prior
    # `npci_counter` snapshot at clear-time plus { resolved_at,
    # resolution, response_text }. The chat timeline reads from here so
    # past counters stay visible after the active card is dismissed —
    # critical for non-repudiation in multi-round negotiation.
    npci_counter_history: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Append-only log of NPCI decisions on partner counters: each entry
    # is {decision: 'ACCEPT'|'REJECT', round, response_text, received_at,
    # in_response_to}. JSON-encoded list. The chat-style UI surfaces
    # these as a "PM responses" section.
    counter_decisions: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Append-only list of blockers we've reported to NPCI + any
    # resolution they've sent back. Each entry is structured:
    #   {blocker_id, severity, description, impact, options_considered,
    #    requested_action_from_npci, created_at, status: 'open'|'resolved',
    #    resolution: {action_taken, resolution_text, artifact_ref,
    #                 resolved_at} | null}
    blockers: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Append-only list of post-freeze emergency issues we've raised + any
    # resolution NPCI sent back. Each entry: {issue_id, severity, title,
    # description, status, resolution, created_at}. Only usable once the
    # change is frozen (negotiation_version >= 3).
    emergency_issues: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Cert lifecycle status. Values: 'received' | 'deployed' | 'tested' |
    # 'ready_for_certification'. Auto-set to 'received' when the change
    # arrives via change_communication; bank user advances through the
    # remaining three from the partner UI. Each transition fires an A2A
    # CERT_STATUS_UPDATE to NPCI via the SDK.
    cert_status: Mapped[str] = mapped_column(String(40), nullable=False, default="received")
    # Append-only ISO-timestamp map for each cert status transition,
    # JSON-encoded. Shape: {"received": "<iso>", "deployed": "<iso>", …}.
    # Used by the partner UI to show timestamps on the lifecycle stepper.
    cert_status_history: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Latest cert_test_response payload from NPCI — JSON-encoded.
    # See partner_handlers.handle_cert_test_response for the shape.
    cert_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ── Negotiation governance (Phase: partner negotiation) ───────────────
    # Mirrors ChangeRequest.negotiation_version on the NPCI side.
    # Set to 1 on receipt; bumped when NPCI sends a new version.
    # Partner UI shows a "New version available" banner when this > 1
    # and the partner hasn't explicitly accepted the current version.
    negotiation_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # True once partner has explicitly accepted the current negotiation_version.
    # Resets to False when negotiation_version increments.
    negotiation_version_accepted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # When NPCI finalised the negotiation (specs locked). NULL = still open.
    negotiation_finalized_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    # NPCI's "summary of changes" shipped in the kit envelope (change_summary):
    # what changed in this kit version. Shown as a banner on the change page.
    npci_change_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # True while NPCI is preparing a revised kit (round closed → new version
    # shipped). Queries are held during this window. Set by the
    # revision_in_progress handler; cleared when the new kit arrives.
    revision_in_progress: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # The version NPCI is building (N+1), for the hold banner text.
    revision_target_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Follow-up NPCI replies to an already-answered general question.
    # The FIRST answer to a question rides on OutgoingQuery.response; any
    # further replies NPCI sends to the SAME question are appended here so
    # the timeline shows every reply (mirroring how NPCI keeps each
    # PO_APPROVED message) instead of the newest answer clobbering the
    # previous one. JSON-encoded list; each entry:
    #   {query_id, message, received_at}
    npci_followups: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Append-only log of NPCI round-lifecycle notices (currently: PM
    # force-closed a negotiation round). The partner has no round UI, so
    # these surface in the change timeline to keep the partner aware that
    # the negotiation window shut. JSON-encoded list; each entry:
    #   {negotiation_round, message, closed_at, received_at}
    round_notices: Mapped[str | None] = mapped_column(Text, nullable=True)
    # NPCI Certification Result certificate (.docx) shipped on the all-PASS
    # cert_completion_signoff A2A task. Stored verbatim (BLOB) so the partner
    # serves the exact bytes NPCI signed, with the filename for the download
    # Content-Disposition. NULL until a signoff arrives.
    cert_signoff_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    cert_signoff_docx_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)


class ChangeDocument(Base):
    __tablename__ = "change_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    change_id: Mapped[str] = mapped_column(String(36), nullable=False)
    doc_type: Mapped[str] = mapped_column(String(50), nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Which published kit version (mirrors NPCI ChangeRequest.negotiation_version)
    # this doc arrived in. A revision APPENDS new rows tagged with the higher
    # version, so v1 docs stay viewable alongside v2 in the partner UI.
    negotiation_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # When the NPCI side has a `.docx` rendition for this doc (docgen
    # pipeline output) it ships the bytes inline as base64 in the
    # change_communication payload. We store both the filename (for
    # Content-Disposition) and the raw bytes (BLOB) so the UI can
    # offer a download without bouncing back to NPCI.
    docx_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    docx_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # D7 — companion .pptx (for product_deck today; any future doc type
    # that ships a presentation also lands here).
    pptx_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    pptx_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # cert_test_cases — companion .xlsx rendered by the NPCI
    # excel_testcase_engine. Stored verbatim so the partner serves
    # the exact NPCI workbook (Index / Summary / Modes / archetype
    # sheets), not a markdown-reassembled approximation.
    xlsx_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    xlsx_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # Promo/explainer video — the PM-uploaded MP4 shipped with the kit. Stored
    # verbatim so the partner UI can play + download it without bouncing to NPCI.
    video_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    video_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # XSD multi-schema bundle — when NPCI's change touches ≥2 .xsd files it ships
    # them zipped (xsd_zip_b64) so the partner downloads ALL schemas, not just the
    # first fenced block the native extractor returns. Single-schema changes ship
    # no zip and keep the native .xsd download.
    zip_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    zip_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

class OutgoingQuery(Base):
    __tablename__ = "outgoing_queries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    change_id: Mapped[str] = mapped_column(String(36), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    response: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="sent")
    # Channel discriminator — 'general' (Phase C clarifications) or
    # 'cert' (cert messaging). Inbound CLARIFICATION_RESPONSE handlers
    # filter by this so a general response never lands in the cert
    # inbox and vice versa.
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="general")
    # Partner-minted UUID per query; echoed by NPCI on the matching
    # CLARIFICATION_RESPONSE so the inbound handler attaches the answer
    # to THIS row, not "most recent in channel". NULL on rows that
    # pre-date the column.
    correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    # When NPCI's CLARIFICATION_RESPONSE landed on this row. NULL until
    # answered — rendered as the response bubble's timestamp on the
    # partner timeline. Previously the FE faked it as `sent_at`, which
    # bunched late replies next to the original query and broke the
    # chronological feel of the chat.
    response_received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # ── Negotiation request categorisation (Phase: partner negotiation) ────
    # Structured category chosen by partner when raising a counter-proposal:
    # timeline | scope | limits | api_contract | dependency | cert_role
    # NULL for general clarification queries (kind='general', kind='cert').
    request_category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Structured payload for the chosen category — JSON-encoded.
    # Shape depends on request_category:
    #   timeline:     {current_date, proposed_date, reason}
    #   scope:        {excluded_flows, phased_rollout, details}
    #   limits:       {field_name, current_value, proposed_value}
    #   api_contract: {field_or_endpoint, change_type, proposed_value}
    #   dependency:   {dependency_name, blocker_description, expected_resolution_date}
    #   cert_role:    {proposed_role, proposed_cert_timeline}
    request_payload: Mapped[str | None] = mapped_column(Text, nullable=True)


class ProgressReport(Base):
    __tablename__ = "progress_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    change_id: Mapped[str] = mapped_column(String(36), nullable=False)
    step: Mapped[str] = mapped_column(String(50), nullable=False)
    reported_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class ChangeTestData(Base):
    """Per-change, per-TC test data the partner submits with the
    "Ready for Certification" status update.

    The partner UI shows one form per NPCI-initiated TC discovered from
    the cert_test_cases ChangeDocument. Bank-initiated TCs do not need
    test data here — the bank runs them on its own simulator side and
    drives the test data inside its own switch.

    Persisted as a JSON blob so the schema follows the LLM suggester
    output (payer_vpa, payee_vpa, amount, ifsc, account_number, etc.)
    without a migration per new field. The orchestrator on NPCI side
    merges these into cert-agent's per-TC test_data via PUT.
    """
    __tablename__ = "change_test_data"

    id:         Mapped[str]      = mapped_column(String(36), primary_key=True, default=_uuid)
    change_id:  Mapped[str]      = mapped_column(String(36), nullable=False, index=True)
    tc_id:      Mapped[str]      = mapped_column(String(50), nullable=False)
    test_data:  Mapped[dict]     = mapped_column(JSON, nullable=False, default=dict)
    # Was the test data accepted from the LLM suggester (1) or hand-edited (0)?
    # Surfaced on the partner UI as a small "AI-filled" hint per row.
    ai_suggested: Mapped[bool]   = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now, nullable=False)


class PartnerSetting(Base):
    __tablename__ = "partner_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")


class PartnerProfile(Base):
    """The partner's capability profile (PARTNER.md) — UI-configurable.

    Replaces the read-only filesystem mount as the source of truth: the active
    profile lives here and is editable from the Settings UI (upload a .md or
    edit in-app). The feasibility analyser and the design/code/testing agents
    read it via `agents/_common.read_partner_profile()`, which prefers this row
    and falls back to `settings.partner_profile_path` (seed / fresh-clone).

    One partner per deployment → a single active row (upsert on save). The
    `partner` / `profile_version` frontmatter is denormalised into columns so
    the UI + report `_meta` can read them without re-parsing the markdown.
    """
    __tablename__ = "partner_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    partner_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    profile_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Full markdown — frontmatter + body, exactly as the LLM consumes it.
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # How the active row got here: 'seed' (one-time from the mounted file),
    # 'upload' (a .md uploaded via the UI), or 'edit' (edited in-app).
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="edit")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now, nullable=False
    )


class QueryDraft(Base):
    __tablename__ = "query_drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    change_id: Mapped[str] = mapped_column(String(36), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # status: draft (editable) | sent (promoted to OutgoingQuery) | discarded (soft-deleted)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    # source: auto (LLM-generated) | manual (future: user-added draft)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="auto")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PartnerUser(Base):
    __tablename__ = "partner_users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class AgentRun(Base):
    """Audit row for every agent invocation — partner-side mirror of the
    NPCI backend's `agent_jobs` table.

    Written by `app.agents.base.Agent.execute()` around each `run()` call,
    regardless of binding (`impl:` in-process or `url:` remote). Gives the
    partner ops user + NPCI a queryable record of which agent ran, against
    which change, how long it took, and whether it succeeded — failures land
    here as `status='failed'` rows instead of vanishing into a daemon thread.
    """
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    change_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # How the agent was reached: 'local' (in-process impl:) or 'http' (url:).
    # 'mcp' is reserved for the deferred MCP binding.
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="local")
    # For remote bindings, the url (or, later, the MCP server). NULL for local.
    endpoint: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # running → succeeded | failed.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running", index=True)
    # HTTP status code for 'http' mode runs; NULL otherwise.
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Small, bounded summary of the input (keys + truncated scalars) — never
    # the full payload, to keep the audit table light.
    input_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # The agent's output dict on success.
    result_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Free-form extra context. Attribute named `meta` because SQLAlchemy
    # reserves `metadata` on the declarative Base; the DB column stays `metadata`.
    meta: Mapped[dict] = mapped_column("metadata", JSON, nullable=False, default=dict)


class FeasibilityReport(Base):
    """Structured per-area feasibility assessment of an incoming NPCI change.

    Produced by `app.agents.feasibility` (FeasibilityAgent) against PARTNER.md +
    the change documents. Versioned per change_id — re-runs append a new
    row, callers read the highest version.
    """
    __tablename__ = "feasibility_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    change_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Full report JSON — { one_line_summary, overall_posture, areas[],
    # next_steps[], additional_findings[], _meta{} }. TEXT-encoded JSON to
    # match the partner-side convention (npci_counter, cert_summary, etc.).
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Frontmatter `profile_version` captured at generation time. Lets the UI
    # warn when the profile has been edited since this report was produced.
    profile_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # "{provider}:{model}" string — e.g. "claude:claude-sonnet-4-6". Auditable.
    model_used: Mapped[str | None] = mapped_column(String(100), nullable=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class DesignReport(Base):
    """Partner-side design document for an incoming NPCI change.

    Produced by `app.agents.design` (DesignAgent) against PARTNER.md + the
    change's product-kit documents. Versioned per change_id — re-runs append a
    new row; callers read the highest version. Same shape as FeasibilityReport.
    """
    __tablename__ = "design_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    change_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Full design JSON — { one_line_summary, design_posture, document_markdown,
    # sections[], components_touched[], dependencies[], risks[], open_questions[],
    # _meta{} }. TEXT-encoded JSON, matching the partner-side convention.
    content: Mapped[str] = mapped_column(Text, nullable=False)
    profile_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_used: Mapped[str | None] = mapped_column(String(100), nullable=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class TestReport(Base):
    """Partner-side test plan for an incoming NPCI change.

    Produced by `app.agents.testing` (TestAgent) against PARTNER.md + the change's
    documents (incl. NPCI cert_test_cases) + the design doc. Authoring only — the
    authoritative cert run is delegated to NPCI's orchestrator via the existing
    cert lifecycle. Versioned per change_id; callers read the highest version.
    """
    __tablename__ = "test_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    change_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Full test-plan JSON — { one_line_summary, readiness, test_plan_markdown,
    # suites[], cert_coverage{}, test_data_needed[], open_questions[], _meta{} }.
    content: Mapped[str] = mapped_column(Text, nullable=False)
    profile_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_used: Mapped[str | None] = mapped_column(String(100), nullable=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class CodeReport(Base):
    """Partner-side implementation plan for an incoming NPCI change.

    Produced by `app.agents.code` (CodeAgent). MVP is spec-grounded only — built
    from the design doc + NPCI docs + PARTNER.md, NOT the partner's source code
    (content's _meta.grounded=False). Repository-grounded generation + MR push is
    the later Code-RAG phase. Versioned per change_id; callers read the highest.
    """
    __tablename__ = "code_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    change_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Full plan JSON — { one_line_summary, code_posture, plan_markdown,
    # work_items[], file_changes[], dependencies[], risks[], open_questions[],
    # _meta{grounded} }.
    content: Mapped[str] = mapped_column(Text, nullable=False)
    profile_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_used: Mapped[str | None] = mapped_column(String(100), nullable=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class KnowledgeDoc(Base):
    """A partner knowledge-base document (Document RAG, doc_category='kb').

    The source of truth for an uploaded KB doc (UPI/IUPI specs, NPCI circulars,
    past change kits, internal standards). Its chunks live in `document_chunks`
    keyed by source_key=id; this row holds the original content + metadata so the
    KB can be listed, re-indexed, and deleted. Retrieved cross-change by the
    design/code/test agents.
    """
    __tablename__ = "knowledge_docs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    source: Mapped[str | None] = mapped_column(String(300), nullable=True)  # filename / url / origin
    content: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class CodeRepo(Base):
    """A partner GitLab repository registered for the Code RAG (Phase 3.1).

    Its source is fetched over the GitLab API, chunked + embedded into
    `document_chunks` (doc_category='code', repo_id=this id). The GitLab token is
    NOT stored here — it lives in `partner_settings` (key 'gitlab_token'),
    write-only. `status` tracks the background index job.
    """
    __tablename__ = "code_repos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    gitlab_url: Mapped[str | None] = mapped_column(String(500), nullable=True)   # override global
    gitlab_repo: Mapped[str] = mapped_column(String(500), nullable=False)        # e.g. "group/upi-stack"
    gitlab_branch: Mapped[str] = mapped_column(String(200), nullable=False, default="main")
    languages: Mapped[str | None] = mapped_column(String(200), nullable=True)    # csv, e.g. "java,python"; null = all known
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="idle")  # idle|indexing|indexed|error
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    files_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunks_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_indexed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class CodeMergeRequest(Base):
    """A merge request opened on the partner's repo from a generated code change
    (Code RAG Phase 3.4).

    The code agent's whole-file generation pass turns the latest implementation
    plan into complete file contents; `git_integrator` branches + commits them and
    opens an MR (never auto-merged — the partner's engineers review). One row per
    push; the highest `created_at` for a change is the current MR.
    """
    __tablename__ = "code_merge_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    change_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    repo_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    code_report_version: Mapped[int | None] = mapped_column(Integer, nullable=True)  # plan version pushed
    branch: Mapped[str] = mapped_column(String(300), nullable=False)
    # Retained for existing rows and for operator/debug use, but NO LONGER SENT
    # to the browser — see _mr_view() in api/dashboard/code.py. Checkmarx traced
    # this value from the API response into an <a href>, tripping Client DOM XSS,
    # Reflected XSS and Client DOM Open Redirect. The UI now rebuilds the link
    # from `project_path` + `mr_iid` against a build-time base URL.
    mr_url: Mapped[str | None] = mapped_column(String(700), nullable=True)
    mr_iid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # GitLab namespace ('group/project'), stored so the browser can rebuild the
    # MR link without the server handing it a URL string.
    project_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    files_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="opened")  # opened|error
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class AgentJob(Base):
    """An in-flight background agent run (the 202 + poll pattern).

    The long agent endpoints (design/code/testing analyse, Open MR) return 202
    with a job id and run in a BackgroundTask; the UI polls the latest job for
    (change_id, kind) to drive its running/progress/error state. The produced
    artifact still lands in its own table (design_reports / code_reports /
    test_reports / code_merge_requests) — this row is only execution state.
    Distinct from `agent_runs`, which audits completed agent executions.
    """
    __tablename__ = "agent_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    change_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # design|code|testing|mr
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="running")  # running|done|error
    progress: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Structured classification of `error` (Finding 15:
    # security_architecture_skills.md §5.3/§14.4, EA_Skills.md P8) — additive
    # to the free-text `error` field, never a replacement for it. NULL for
    # successful jobs or jobs that predate error classification.
    # error_category: business | technical | resource_access | security
    error_category: Mapped[str | None] = mapped_column(String(20), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Total LLM tokens (input+output, summed across every call_llm() made by
    # this job's runner — see core.llm.track_token_usage()) spent by this run.
    # Feeds the per-change budget guard (core/llm_budget.py, Finding 4:
    # security_architecture_skills.md §4.2, EA_Skills.md P6/P10). NULL for
    # jobs that predate token tracking.
    tokens_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # One correlation id per job, auto-generated at creation, threaded through
    # to every outbound A2A call the job's runner triggers (npci_client.py's
    # `correlation_id=` parameter) and available for LLM/GitLab call sites to
    # pick up too (Finding 13: security_architecture_skills.md §13.1 —
    # correlation IDs MUST propagate across sync calls, async messages,
    # internal modules, and dependency calls). Distinct from the A2A
    # envelope's own `correlation_id` field (the NPCI thread pointer) — this
    # is the platform's OWN causal-chain id for "why did this specific
    # outbound send/LLM call happen," independent of which NPCI conversation
    # thread it rides on.
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False, default=_uuid)


class GeneratedCodeFile(Base):
    """One whole-file output of the code-gen step, persisted so it can be
    reviewed (code-review + security-review loop) BEFORE the merge request is
    opened. Previously file contents were generated only at MR time and pushed
    directly; splitting generation from the push makes the files a reviewable
    artifact. Rows are grouped by `iteration` — "Apply Fixes" regenerates into a
    new iteration so prior versions stay inspectable. The MR step pushes the
    latest iteration's files verbatim.
    """
    __tablename__ = "generated_code_files"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    change_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    iteration: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    path: Mapped[str] = mapped_column(String(700), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 'create' | 'modify' — informational (the git step decides create/update by
    # checking existence on the branch); carried from the plan's file_changes.
    action: Mapped[str | None] = mapped_column(String(20), nullable=True)
    code_report_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Stable ids (see review_base._finding_id) of the findings this iteration's
    # regeneration was fixing — e.g. ["F-3a1b2c...", ...]. NULL for iteration 1
    # (nothing to fix yet — the initial generation). SDLC Gap 8 (docs/
    # ARCHITECTURE_REVIEW_ACTIONS.md, Tier 3): gives the platform a persisted,
    # queryable audit trail from finding -> fix, distinct from the UI's
    # best-effort title/file-matching heuristic in _review_history().
    fixed_finding_refs: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class CodeReviewReport(Base):
    """One reviewer's findings over a generated-file `iteration`. The review step
    runs two agents (reviewer = 'code_quality' and 'security') over the same
    iteration, writing one row each; the UI merges them. `content` is the agent's
    findings JSON ({summary, findings:[{severity,category,file,line,title,detail,
    suggested_fix}]}). Loop state is DERIVED, not stored: the current iteration is
    max(generated_code_files.iteration); the reviewed iteration is
    max(code_review_reports.iteration); a review blocks (issues_found) when the
    reviewed iteration has any finding. MR is allowed only when reviewed ==
    current and there are zero findings.
    """
    __tablename__ = "code_review_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    change_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    iteration: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    reviewer: Mapped[str] = mapped_column(String(20), nullable=False)  # code_quality | security
    content: Mapped[str] = mapped_column(Text, nullable=False)         # findings JSON
    model_used: Mapped[str | None] = mapped_column(String(100), nullable=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class OutboundA2ARetry(Base):
    """A failed `npci_client.send_task()` call, queued for retry — the
    platform's equivalent of a DLQ for the partner->NPCI direction (Finding
    12: security_architecture_skills.md §5.4/§11.3, EA_Skills.md P7 "DLQ and
    replay process"). Closes the "silently dropped outbound message" gap:
    previously a transient NPCI outage or an open circuit breaker meant
    `send_task()` returned None with nothing but a log line, and the
    operator's only recourse was noticing the UI error and manually retrying
    the original action.

    `attempts` and `next_retry_at` drive an exponential-backoff sweep
    (services/outbound_retry.py); a row moves pending -> delivered (success)
    or pending -> abandoned (attempts exhausted — surfaced as a security/ops
    event for operator attention, per docs/OPERATIONAL_RUNBOOKS.md §3.6).
    """
    __tablename__ = "outbound_a2a_retries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    change_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    # Correlation id threaded from the originating job/call (Finding 13) so an
    # abandoned retry can still be traced back to the AgentJob that triggered it.
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # The envelope `message_id` every attempt of this row re-sends under — the
    # receiver's dedup key (EA_Skills.md P3 "Idempotent operations and safe
    # retries"). Without it a retry looked like a new message and could be
    # processed twice by NPCI when only the ACK, not the send, was lost.
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")  # pending|delivered|abandoned
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CertFixRound(Base):
    """One round of certification-defect remediation (CERT-5).

    The authority sends ONE cert_verdict_notification per failing case; they
    are one round's work — `open_round` APPENDS to the open round, so five
    failures produce one fix job, not five. A NEW table (create_all builds it
    on startup; the `_ensure_*` column helpers are for columns added to
    EXISTING tables only).

    status: open → fixing → awaiting_approval → approved, with
    awaiting_manual_fix / fix_failed as honest stops — a cert verdict names an
    API and an xpath, never a source file, so the automated fix can only run
    once findings are mapped to files; until then the operator fixes manually
    and marks the round fixed. `send_cert_fix_notification` is reachable from
    exactly one place: the approval endpoint.
    """
    __tablename__ = "cert_fix_rounds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    change_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    cflow_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    round_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Failing case ids, appended per verdict; deduplicated.
    verdict_case_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # The verdict payloads as received (classification, assertion_failures…) —
    # the input `verdicts_to_findings` converts.
    verdicts: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    fix_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    fix_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="open")
    review_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now, nullable=False)

    __table_args__ = (
        # One round number per change, enforced by the DATABASE. `open_round`
        # is a select-then-insert, and two verdicts for the same batch
        # arriving concurrently both saw "no open round" and both created
        # round 1 — one verdict batch became two round-1 jobs, destroying
        # remediation attribution and sequencing. The constraint turns that
        # race into an IntegrityError the loser retries into the winner's row.
        UniqueConstraint("change_id", "round_number",
                         name="uq_cert_fix_rounds_change_round"),
    )


class IntegrationExchange(Base):
    """One row per tunnelled HTTP exchange (ITA I-9).

    The bar: a failed exchange is diagnosable from the row alone — alias,
    method/path, bytes each way, timing, and the §5.2 error code when it
    failed. `correlation_id` carries the exchange id that also rode the A2A
    hop (architecture review A12). New table → `create_all` builds it.
    """
    __tablename__ = "integration_exchanges"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    exchange_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)   # ingress | egress
    alias: Mapped[str] = mapped_column(String(100), nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    path: Mapped[str] = mapped_column(String(1000), nullable=False)
    # The query string, stored VERBATIM — never parsed, reordered or re-encoded,
    # exactly as `integration_contract` already carries it on the wire.
    #
    # Added for NET-F21. Without it, two exchanges differing ONLY by their query
    # were indistinguishable here, and contract selection in the certification
    # flow rides entirely on `?pack=`. The documented failure mode — a normalised
    # or dropped selector presenting later as "certified against baseline"
    # rather than as an error — lived in the one field the telemetry could not
    # see. Recording it verbatim makes byte-fidelity auditable AFTER the fact
    # rather than only testable before it.
    #
    # NULL and "" mean different things and the distinction is load-bearing:
    # "" is a hop that genuinely carried no query; NULL is a row written before
    # this column existed, i.e. "not recorded". Collapsing them would reintroduce
    # the very ambiguity this column exists to remove.
    query: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    request_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    response_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Header NAMES this hop dropped — values deliberately not recorded; some
    # of them are credentials.
    dropped_headers: Mapped[list | None] = mapped_column(JSON, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cert_context: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class CertCaseExecution(Base):
    """One executed certification case, as the RIG reported it — the partner's
    own durable record of what its application was asked, what it answered,
    and how the round's contract graded it.

    This is the partner half of full-trace observability. The tunnel records
    byte COUNTS only (deliberately — bodies can carry credentials on other
    routes), and the authority holds its own copy inside the cert run rows;
    without this table the partner forwarded its verdict evidence and kept
    nothing. `details` carries the rig's whole report verbatim: observed vs
    expected, grading reasons, sim pack/verdict, and the capped raw payloads
    (sut_request / sut_response / sim_response).

    NEW table, so plain `create_all` provisions it (the platform has no
    alembic); only added COLUMNS would need an `_ensure_*` helper.
    """
    __tablename__ = "cert_case_executions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    npci_change_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    case_id: Mapped[str] = mapped_column(String(50), nullable=False)
    cert_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    cflow_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)   # passed|failed|error
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
