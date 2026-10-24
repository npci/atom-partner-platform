"""Partner Platform configuration."""
import logging

from pydantic_settings import BaseSettings, SettingsConfigDict

_config_logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    app_env: str = "development"  # development | staging | production
    partner_name: str = "Partner Agent"
    npci_platform_url: str = "http://localhost"
    partner_api_key: str = ""
    # Main partner DB — moved from SQLite to Postgres (see
    # docs/PARTNER_POSTGRES_MIGRATION_PLAN.md). The default below matches
    # the `partner_postgres` service in the root docker-compose.yml so a
    # fresh clone works without setting up `.env`. Operators override via
    # `DATABASE_URL` env (typical) or by setting `PARTNER_POSTGRES_*` and
    # letting compose interpolate.
    database_url: str = "postgresql+psycopg://partner_user:partner_password@partner_postgres:5432/partner_db"
    port: int = 8001
    # Public URL the partner advertises in its AgentCard's
    # supported_interfaces[0].url. The A2A SDK posts JSON-RPC to this
    # URL verbatim — httpx requires the http:// protocol prefix, so a
    # bare path won't work. Default points at the docker service name
    # so east-west calls from npci_backend resolve; production overrides
    # via PARTNER_PUBLIC_URL env (e.g. https://bank.example.com/a2a-partner).
    partner_public_url: str = "http://partner_backend:8001"

    # ── LLM provider routing ────────────────────────────────────────
    # Mirrors the NPCI backend's `core/llm.py` shape: pick the provider
    # via `LLM_PROVIDER` env, then per-provider key + model + (for AiNxt)
    # base_url. Trimmed to three providers — partner stack doesn't run
    # Ollama. Default is Claude so existing partner deployments keep
    # working without an env edit.
    llm_provider: str = "claude"  # claude | openai | ainxt

    # Anthropic
    partner_anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-4-6"

    # OpenAI
    partner_openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    # AiNxt — NPCI internal gateway. Two wire modes (mirrors the NPCI backend):
    #   "openai"    → /chat/completions (OpenAI-compatible; today's default)
    #   "anthropic" → /v1/messages (Anthropic-compatible; native tool_use)
    # In anthropic mode the Anthropic SDK is pointed at ainxt_base_url and
    # authenticates via Authorization: Bearer (auth_token), and the model is
    # ainxt_messages_model (AiNxt normalises aliases).
    partner_ainxt_api_key: str = ""
    ainxt_base_url: str = "https://ainxt.npci.org.in/ainxt/v1/api"
    ainxt_model: str = "gpt-4o"
    ainxt_compat_mode: str = "openai"                  # "openai" | "anthropic"
    ainxt_messages_model: str = "claude-sonnet-4-6"    # model when ainxt_compat_mode="anthropic"

    # ── Feasibility analyser ─────────────────────────────────────────
    # Path to the partner's PARTNER.md profile. Loaded as context by
    # the feasibility analyser when an incoming change is evaluated.
    # Bind-mounted into the container by compose from the host path
    # `partner-platform/data/partner_profile.md`. Override via
    # PARTNER_PROFILE_PATH for non-standard deployments.
    partner_profile_path: str = "/app/data/partner_profile.md"

    # ── RAG / embeddings (Code-RAG + Document-RAG, Phase 3) ──────────
    # Shared vector foundation: a partner-side Ollama serves the embedding
    # model; vectors land in pgvector (`document_chunks`) on partner_postgres.
    # Both the Document RAG (NPCI docs + knowledge base) and the Code RAG
    # (partner repo) use this. nomic-embed-text is 768-dim, matching the
    # `vector(768)` columns; keep embed_dim in lockstep with the column width.
    embed_provider: str = "ollama"
    embed_model: str = "nomic-embed-text"
    embed_dim: int = 768
    ollama_url: str = "http://ollama:11434"
    # Per-request embed batch size for the Ollama /api/embed call.
    embed_batch_size: int = 64

    # ── Code RAG (Phase 3.1) — partner's own GitLab repo ─────────────
    # Default GitLab base URL; a CodeRepo row may override per-repo. The
    # access token is NOT here — it is stored write-only in partner_settings
    # (key 'gitlab_token') via the Settings/Code-repo UI.
    partner_gitlab_url: str = "https://gitlab.com"
    # Cap on the number of source files pulled per index (safety bound for a
    # large repo; 0 = unlimited).
    code_index_max_files: int = 4000

    # ── Agent framework ──────────────────────────────────────────────
    # Path to the YAML manifest that wires each agent (impl:/url:) +
    # prompt + per-agent model/provider. Empty → the loader falls back
    # to `<backend>/config/agents.yaml`. Override via AGENTS_CONFIG env.
    agents_config: str = ""
    # "Apply fixes" auto-loops fix → review until zero findings. This caps the
    # rounds so a non-converging review (e.g. a persistent false positive) can't
    # run forever / burn unbounded LLM cost. On hitting the cap the loop stops
    # and leaves the change in issues_found (the operator can re-run or edit).
    # Override via CODE_REVIEW_MAX_FIX_ROUNDS.
    code_review_max_fix_rounds: int = 5
    # Local login-session signing key (distinct from the NPCI-issued
    # `npci_jwt_secret` used for inbound A2A). MUST be set per deployment;
    # the app warns loudly when left unset. Override via SESSION_JWT_SECRET env.
    # Default is empty (not a placeholder literal) — an unset secret is detected
    # by emptiness, which also keeps a hardcoded-credential string out of source.
    session_jwt_secret: str = ""

    # ── Txn over A2A wrappers (docs/upi_over_a2a_plan.md) ───────────────────
    # The Egress listener the partner's switch posts its NPCI-bound traffic to
    # (`https://<host>:8111/t/upi/{org}`). TLS, because switches such as the
    # simulator hardcode https://. Generate the pair with
    # scripts/gen-wrapper-certs.sh.
    txn_egress_port: int = 8111
    txn_egress_ssl_cert: str = "../../certs/wrapper/wrapper.crt"
    txn_egress_ssl_key: str = "../../certs/wrapper/wrapper.key"
    # Whether startup launches the Egress listener in-process (it must share the
    # process — txn_state is in-memory). Tests turn it off to avoid port clashes.
    txn_egress_autostart: bool = True
    # The partner's own hop log (NPCI never sees these internal legs).
    txn_hop_log_path: str = "./txn_hops.jsonl"
    # Ladder (plan, Risks): switch ACK < this A2A call < engine read timeouts.
    txn_a2a_call_timeout_s: float = 8.0
    txn_switch_post_timeout_s: float = 12.0


settings = Settings()

# ── Fail-fast: refuse to start production with an unset JWT secret ──
if not settings.session_jwt_secret:
    if settings.app_env == "production":
        raise RuntimeError(
            "SESSION_JWT_SECRET is unset. "
            "Set a strong, per-deployment value before running in production."
        )
    _config_logger.warning(
        "SESSION_JWT_SECRET is unset — using an INSECURE default. "
        "Set a strong value via the SESSION_JWT_SECRET env var before production."
    )
