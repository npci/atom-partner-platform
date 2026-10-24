"""Partner platform database — Postgres.

Moved from SQLite (file-backed, single-writer) to a dedicated
Postgres instance (`partner_postgres` service in the root
docker-compose.yml). See `docs/PARTNER_POSTGRES_MIGRATION_PLAN.md`.

Schema management remains lightweight (no alembic on partner side):
`Base.metadata.create_all()` builds new tables and the
`_ensure_*_columns()` helpers below ADD COLUMN IF NOT EXISTS the
incrementally-added columns. Postgres-native `ADD COLUMN IF NOT EXISTS`
(PG 9.6+) keeps the helpers idempotent without the PRAGMA-table_info
introspection the SQLite version needed.
"""
import logging

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

logger = logging.getLogger(__name__)

engine = create_engine(
    settings.database_url,
    # Sane pool defaults for a single-instance dev backend. The pool
    # is per-process; the SDK's aiosqlite task store has its own pool.
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    # Import models so SQLAlchemy registers their tables on `Base.metadata`
    # before `create_all()` runs. Import order doesn't matter.
    from app.models import (  # noqa: F401
        AgentJob,
        AgentRun,
        ChangeDocument,
        ChangeTestData,
        CodeMergeRequest,
        CodeReport,
        CodeRepo,
        CodeReviewReport,
        DesignReport,
        FeasibilityReport,
        GeneratedCodeFile,
        IncomingChange,
        KnowledgeDoc,
        OutgoingQuery,
        PartnerProfile,
        PartnerSetting,
        PartnerUser,
        ProgressReport,
        QueryDraft,
        TestReport,
    )
    Base.metadata.create_all(bind=engine)
    _ensure_change_documents_docx_columns()
    _ensure_change_documents_pptx_columns()
    _ensure_change_documents_xlsx_columns()
    _ensure_change_documents_video_columns()
    _ensure_change_documents_zip_columns()
    _ensure_change_documents_negotiation_version_column()
    _ensure_incoming_changes_decision_column()
    _ensure_incoming_changes_correlation_id_column()
    _ensure_outgoing_queries_kind_column()
    _ensure_outgoing_queries_correlation_id_column()
    _ensure_outgoing_queries_response_received_at_column()
    _ensure_incoming_changes_cert_status_columns()
    _ensure_incoming_changes_npci_counter_history_column()
    _ensure_outgoing_queries_negotiation_columns()
    _ensure_incoming_changes_negotiation_columns()
    _ensure_incoming_changes_npci_followups_column()
    _ensure_incoming_changes_round_notices_column()
    _ensure_incoming_changes_emergency_issues_column()
    _ensure_incoming_changes_change_summary_column()
    _ensure_incoming_changes_revision_hold_columns()
    _ensure_incoming_changes_cert_signoff_columns()
    # RAG / vector foundation (Phase 3). Extension first, then the shared
    # pgvector tables both the Document RAG and the Code RAG write to.
    _ensure_pgvector_extension()
    _ensure_embedding_cache_table()
    _ensure_document_chunks_table()
    _ensure_seed_partner_profile()
    _sweep_interrupted_agent_jobs()


def _ensure_seed_partner_profile() -> None:
    """One-time seed of the active `partner_profiles` row from the mounted file.

    The partner profile is now DB-backed + UI-editable (see PartnerProfile +
    agents/_common.read_partner_profile). To preserve the old "mount a .md, it
    just works" behaviour and give every fresh deployment an editable starting
    point, on first boot we copy the file at `settings.partner_profile_path`
    into the table — but only when the table is empty, so a partner's edits are
    never clobbered on restart.

    Best-effort: a failure here must not abort startup (the loader still falls
    back to the file at read time).
    """
    from pathlib import Path

    try:
        path = Path(settings.partner_profile_path)
        if not path.exists():
            return
        raw = path.read_text(encoding="utf-8")
        if not raw.strip():
            return

        from app.models import PartnerProfile

        with SessionLocal() as db:
            if db.query(PartnerProfile).first() is not None:
                return
            fm = _parse_flat_frontmatter(raw)
            db.add(PartnerProfile(
                partner_name=fm.get("partner"),
                profile_version=fm.get("profile_version"),
                content=raw,
                source="seed",
            ))
            db.commit()
            logger.info(
                "Seeded partner_profiles from %s (partner=%r version=%r)",
                path, fm.get("partner"), fm.get("profile_version"),
            )
    except Exception as e:  # noqa: BLE001 — never block startup on the seed
        logger.warning("partner_profiles seed skipped", exc_info=True)


def _parse_flat_frontmatter(raw: str) -> dict:
    """Minimal `key: value` frontmatter reader (same flat shape the partner
    profile uses elsewhere — see api/feasibility.py._parse_frontmatter). Inlined
    here to keep the DB layer free of an agents-layer import."""
    if not raw.startswith("---"):
        return {}
    end = raw.find("\n---", 3)
    if end < 0:
        return {}
    out: dict[str, str] = {}
    for line in raw[3:end].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _sweep_interrupted_agent_jobs() -> None:
    """A restart kills in-flight BackgroundTasks; mark their job rows so the UI
    shows a clear error instead of a forever-spinner."""
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE agent_jobs SET status='error', "
                "error='interrupted by a server restart — run again', "
                "finished_at=now() WHERE status='running'"
            ))
    except Exception as e:  # noqa: BLE001 — table may not exist on first boot
        logger.warning("agent_jobs restart sweep skipped", exc_info=True)


def _ensure_pgvector_extension() -> None:
    """Enable the pgvector extension. Requires the `pgvector/pgvector:pg16`
    image (alpine postgres does not ship it) and a DB role that may CREATE
    EXTENSION (the compose POSTGRES_USER is the DB owner/superuser).

    Best-effort: a failure here (wrong image / insufficient privilege) is logged
    but does not abort startup, so the non-RAG partner features still boot. The
    vector tables below will then no-op-fail until the extension is present."""
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "pgvector extension not enabled (RAG features will be unavailable "
            "until the partner_postgres image is pgvector/pgvector:pg16): %s", e
        )


def _ensure_embedding_cache_table() -> None:
    """Cache of (content_sha256, embedding_model) → vector. See rag/embed_cache.py."""
    dim = settings.embed_dim
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                f"""
                CREATE TABLE IF NOT EXISTS embedding_cache (
                    content_sha256 CHAR(64)     NOT NULL,
                    model          TEXT         NOT NULL,
                    embedding      vector({dim}) NOT NULL,
                    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
                    PRIMARY KEY (content_sha256, model)
                )
                """
            )
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("embedding_cache table not created: %s", e)


def _ensure_document_chunks_table() -> None:
    """Shared chunk store for both RAGs. `doc_category` distinguishes
    'change_doc' / 'kb' (Document RAG) from 'code' (Code RAG); `change_id` /
    `repo_id` scope retrieval. No ANN index yet — exact cosine scan is fine at
    partner volumes; add an HNSW index here once chunk counts grow."""
    dim = settings.embed_dim
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                f"""
                CREATE TABLE IF NOT EXISTS document_chunks (
                    id             VARCHAR(36)  PRIMARY KEY,
                    doc_category   VARCHAR(40)  NOT NULL,
                    source_key     VARCHAR(512),
                    change_id      VARCHAR(36),
                    repo_id        VARCHAR(36),
                    chunk_index    INTEGER      NOT NULL DEFAULT 0,
                    content        TEXT         NOT NULL,
                    content_sha256 CHAR(64),
                    embedding      vector({dim}) NOT NULL,
                    metadata       JSONB,
                    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
                )
                """
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_document_chunks_category ON document_chunks (doc_category)"
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_document_chunks_change ON document_chunks (change_id)"
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_document_chunks_repo ON document_chunks (repo_id)"
            )
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("document_chunks table not created: %s", e)


def _ensure_change_documents_docx_columns() -> None:
    """Backfill `change_documents.docx_filename` + `docx_bytes` when missing.

    Idempotent via Postgres `ADD COLUMN IF NOT EXISTS` (PG 9.6+).
    `BYTEA` is Postgres's binary type — SQLAlchemy's `LargeBinary`
    maps to it transparently on the model side.
    """
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE change_documents ADD COLUMN IF NOT EXISTS docx_filename VARCHAR(500)"
        )
        conn.exec_driver_sql(
            "ALTER TABLE change_documents ADD COLUMN IF NOT EXISTS docx_bytes BYTEA"
        )


def _ensure_change_documents_pptx_columns() -> None:
    """Companion `.pptx` (Product Deck — D7). Same shape as docx."""
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE change_documents ADD COLUMN IF NOT EXISTS pptx_filename VARCHAR(500)"
        )
        conn.exec_driver_sql(
            "ALTER TABLE change_documents ADD COLUMN IF NOT EXISTS pptx_bytes BYTEA"
        )


def _ensure_change_documents_video_columns() -> None:
    """Companion `.mp4` (promo/explainer video). Same shape as docx."""
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE change_documents ADD COLUMN IF NOT EXISTS video_filename VARCHAR(500)"
        )
        conn.exec_driver_sql(
            "ALTER TABLE change_documents ADD COLUMN IF NOT EXISTS video_bytes BYTEA"
        )


def _ensure_change_documents_xlsx_columns() -> None:
    """Companion `.xlsx` (cert_test_cases). Same shape as docx/pptx —
    we store the exact NPCI-rendered workbook so the partner ships
    the same bytes NPCI's own download endpoint serves.
    """
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE change_documents ADD COLUMN IF NOT EXISTS xlsx_filename VARCHAR(500)"
        )
        conn.exec_driver_sql(
            "ALTER TABLE change_documents ADD COLUMN IF NOT EXISTS xlsx_bytes BYTEA"
        )


def _ensure_change_documents_zip_columns() -> None:
    """Companion `.zip` (multi-schema XSD bundle). Same shape as docx/xlsx —
    the partner serves the exact zip NPCI built from the frozen Phase-A
    schema files so all `.xsd` files are downloadable, not just the first.
    """
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE change_documents ADD COLUMN IF NOT EXISTS zip_filename VARCHAR(500)"
        )
        conn.exec_driver_sql(
            "ALTER TABLE change_documents ADD COLUMN IF NOT EXISTS zip_bytes BYTEA"
        )


def _ensure_change_documents_negotiation_version_column() -> None:
    """Backfill `change_documents.negotiation_version` — the published kit
    version each doc arrived in. A revision send appends new rows tagged with
    the higher version, so the partner UI can show v1 docs alongside v2.

    MUST exist before the first revision send arrives (the inbound handler
    inserts ChangeDocument rows with this column set).
    """
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE change_documents ADD COLUMN IF NOT EXISTS negotiation_version INTEGER NOT NULL DEFAULT 1"
        )


def _ensure_incoming_changes_decision_column() -> None:
    """Backfill the rollout-decision columns added by the a2acert merge.

    decision tracks the partner-side decision lifecycle:
      pending       — initial state on receipt
      acknowledged  — auto-ack with checksum verification has been sent
      accepted      — partner clicked Accept (PROPOSAL_ACCEPTANCE sent)
      negotiating   — partner clicked Negotiate (COUNTER_PROPOSAL sent)

    npci_counter, counter_decisions, blockers store JSON blobs (as TEXT
    — see migration plan §Out of scope; JSONB upgrade lands as a
    follow-up so this slice stays surgical).
    """
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE incoming_changes ADD COLUMN IF NOT EXISTS decision TEXT NOT NULL DEFAULT 'pending'"
        )
        conn.exec_driver_sql(
            "ALTER TABLE incoming_changes ADD COLUMN IF NOT EXISTS npci_counter TEXT"
        )
        conn.exec_driver_sql(
            "ALTER TABLE incoming_changes ADD COLUMN IF NOT EXISTS counter_decisions TEXT"
        )
        conn.exec_driver_sql(
            "ALTER TABLE incoming_changes ADD COLUMN IF NOT EXISTS blockers TEXT"
        )


def _ensure_incoming_changes_correlation_id_column() -> None:
    """Backfill `correlation_id` — NPCI's per-(change, bank) A2A thread id,
    captured on receipt and echoed on every reply (v1.1 §5). NULL for changes
    received before this column existed / from a legacy NPCI that omits it."""
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE incoming_changes ADD COLUMN IF NOT EXISTS correlation_id TEXT"
        )


def _ensure_incoming_changes_cert_status_columns() -> None:
    """Backfill `cert_status`, `cert_status_history`, and `cert_summary`
    on `incoming_changes`.

    cert_status drives the cert lifecycle stepper on the partner UI;
    cert_status_history (JSON-encoded TEXT) records the timestamp at
    each transition for the visual timeline; cert_summary (also
    JSON-encoded TEXT) carries the per-run pass/fail breakdown NPCI
    publishes back via `cert_test_response` after the orchestrator
    completes (see `partner_handlers.handle_cert_test_response`).
    """
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE incoming_changes ADD COLUMN IF NOT EXISTS cert_status TEXT NOT NULL DEFAULT 'received'"
        )
        conn.exec_driver_sql(
            "ALTER TABLE incoming_changes ADD COLUMN IF NOT EXISTS cert_status_history TEXT"
        )
        conn.exec_driver_sql(
            "ALTER TABLE incoming_changes ADD COLUMN IF NOT EXISTS cert_summary TEXT"
        )


def _ensure_incoming_changes_cert_signoff_columns() -> None:
    """Backfill the NPCI Certification Result certificate columns on
    `incoming_changes`.

    `cert_signoff_docx_bytes` (BYTEA) holds the .docx NPCI ships on the
    all-PASS `cert_completion_signoff` A2A task; `cert_signoff_filename`
    is its download filename. See handlers/cert_completion_signoff.py.
    """
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE incoming_changes ADD COLUMN IF NOT EXISTS cert_signoff_filename VARCHAR(500)"
        )
        conn.exec_driver_sql(
            "ALTER TABLE incoming_changes ADD COLUMN IF NOT EXISTS cert_signoff_docx_bytes BYTEA"
        )


def _ensure_outgoing_queries_kind_column() -> None:
    """Backfill `outgoing_queries.kind` channel discriminator.

    Values: 'general' (default — Phase C clarification queries) or
    'cert' (cert messaging channel). The cert messaging UI filters on
    this column so the two inboxes never overlap.
    """
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE outgoing_queries ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'general'"
        )


def _ensure_incoming_changes_npci_counter_history_column() -> None:
    """Backfill `incoming_changes.npci_counter_history` — append-only JSON
    list of every NPCI counter-proposal this partner has received, with
    each entry tagged by how the partner resolved it.

    WHY: `npci_counter` is a single-slot active-counter pointer; it gets
    nulled the moment the partner clicks Accept / Counter / etc. so the
    "needs action" card disappears. Before this column, that null
    silently destroyed the conversation history — fine for UX state, bad
    for audit / non-repudiation. With this column, the active slot still
    gets cleared, but the resolved counter snapshot is appended here
    first so the chat timeline keeps showing it.

    Entry shape: { counter_proposal_id, negotiation_round, justification,
    valid_until, received_at, resolved_at, resolution: 'accepted' |
    'countered' | 'accepted_rollout', response_text }.
    """
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE incoming_changes ADD COLUMN IF NOT EXISTS npci_counter_history TEXT"
        )


def _ensure_outgoing_queries_response_received_at_column() -> None:
    """Backfill `outgoing_queries.response_received_at` — captured when
    NPCI's CLARIFICATION_RESPONSE lands so the partner timeline can
    render the response bubble at the time it actually arrived, not
    sorted next to the original query.
    """
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE outgoing_queries ADD COLUMN IF NOT EXISTS response_received_at TIMESTAMP"
        )


def _ensure_outgoing_queries_negotiation_columns() -> None:
    """Backfill structured negotiation request columns on `outgoing_queries`.

    request_category — partner-selected type for counter-proposals
      (timeline|scope|limits|api_contract|dependency|cert_role).
    request_payload  — JSON-encoded structured fields per category.
    Both are NULL on general clarification queries; set only when
    kind='negotiation' counter-proposal is being raised.
    """
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE outgoing_queries ADD COLUMN IF NOT EXISTS request_category VARCHAR(50)"
        )
        conn.exec_driver_sql(
            "ALTER TABLE outgoing_queries ADD COLUMN IF NOT EXISTS request_payload TEXT"
        )


def _ensure_incoming_changes_negotiation_columns() -> None:
    """Backfill negotiation governance columns on `incoming_changes`.

    negotiation_version          — mirrors NPCI's version counter (starts 1)
    negotiation_version_accepted — True = partner has accepted current version
    negotiation_finalized_at     — ISO timestamp when NPCI locked the specs
    """
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE incoming_changes ADD COLUMN IF NOT EXISTS negotiation_version INTEGER NOT NULL DEFAULT 1"
        )
        conn.exec_driver_sql(
            "ALTER TABLE incoming_changes ADD COLUMN IF NOT EXISTS negotiation_version_accepted BOOLEAN NOT NULL DEFAULT TRUE"
        )
        conn.exec_driver_sql(
            "ALTER TABLE incoming_changes ADD COLUMN IF NOT EXISTS negotiation_finalized_at TEXT"
        )


def _ensure_incoming_changes_npci_followups_column() -> None:
    """Backfill `incoming_changes.npci_followups` — append-only JSON list
    of NPCI's follow-up replies to an already-answered general question.
    The first answer rides on OutgoingQuery.response; later replies are
    appended here so a newer reply doesn't overwrite the older one.
    See IncomingChange.npci_followups + handle_clarification_response.
    """
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE incoming_changes ADD COLUMN IF NOT EXISTS npci_followups TEXT"
        )


def _ensure_incoming_changes_round_notices_column() -> None:
    """Backfill `incoming_changes.round_notices` — append-only JSON list of
    NPCI round-lifecycle notices (PM force-closed a round). Surfaced in the
    partner timeline. See IncomingChange.round_notices + the ROUND_CLOSED
    branch in handle_clarification_response.
    """
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE incoming_changes ADD COLUMN IF NOT EXISTS round_notices TEXT"
        )


def _ensure_incoming_changes_change_summary_column() -> None:
    """Backfill `incoming_changes.npci_change_summary` — the "summary of changes"
    NPCI ships in the kit envelope (change_summary) describing what changed in
    this kit version. Surfaced as a banner on the partner change page.
    """
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE incoming_changes ADD COLUMN IF NOT EXISTS npci_change_summary TEXT"
        )


def _ensure_incoming_changes_revision_hold_columns() -> None:
    """Backfill the revision-hold flags — set while NPCI prepares a revised kit
    (round closed → new version shipped). Queries are held during this window."""
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE incoming_changes ADD COLUMN IF NOT EXISTS "
            "revision_in_progress BOOLEAN NOT NULL DEFAULT FALSE"
        )
        conn.exec_driver_sql(
            "ALTER TABLE incoming_changes ADD COLUMN IF NOT EXISTS revision_target_version INTEGER"
        )


def _ensure_incoming_changes_emergency_issues_column() -> None:
    """Backfill `incoming_changes.emergency_issues` — append-only JSON list of
    post-freeze emergency issues raised to NPCI + resolutions. See the
    emergency-issue endpoint in api/dashboard/defects.py (Slice 5)."""
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE incoming_changes ADD COLUMN IF NOT EXISTS emergency_issues TEXT"
        )


def _ensure_outgoing_queries_correlation_id_column() -> None:
    """Backfill `outgoing_queries.correlation_id` — partner-minted UUID per
    query, echoed by NPCI on the CLARIFICATION_RESPONSE so we attach the
    answer to the exact originating row instead of falling back to "most
    recent in channel" (which silently mis-routed responses when more
    than one query was in flight in the same channel).

    Nullable on purpose: rows that pre-date this column stay NULL and
    fall through to the legacy "most recent" matching path.
    """
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE outgoing_queries ADD COLUMN IF NOT EXISTS correlation_id VARCHAR(36)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_outgoing_queries_correlation_id "
            "ON outgoing_queries (correlation_id)"
        )
