# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Partner Platform configuration."""
import logging
import os

from pydantic_settings import BaseSettings, SettingsConfigDict

_config_logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    app_env: str = "development"  # development | staging | production
    partner_name: str = "Partner Agent"
    # NPCI platform URL — MUST use https:// in any deployment carrying real
    # credentials. The http:// default is accepted only in development for
    # local docker-compose setups; production and staging raise a hard error
    # (see startup guard below). Override via NPCI_PLATFORM_URL env.
    npci_platform_url: str = "http://localhost"
    partner_api_key: str = ""
    # Main partner DB — moved from SQLite to Postgres (see
    # docs/PARTNER_POSTGRES_MIGRATION_PLAN.md). MUST be set via the
    # DATABASE_URL environment variable — there is no default value
    # because a hardcoded password in source code would be visible to
    # everyone with repository access (SAST findings F-002, F-004).
    # The docker-compose.yml sets DATABASE_URL from compose variables
    # so a fresh clone works without manual .env setup.
    database_url: str = ""
    port: int = 8001
    # Public URL the partner advertises in its AgentCard's
    # supported_interfaces[0].url. The A2A SDK posts JSON-RPC to this
    # URL verbatim — httpx requires the http:// protocol prefix, so a
    # bare path won't work. Default points at the docker service name
    # so east-west calls from npci_backend resolve; production overrides
    # via PARTNER_PUBLIC_URL env (e.g. https://bank.example.com/a2a-partner).
    # MUST use https:// in any deployment carrying real credentials.
    partner_public_url: str = "http://partner_backend:8001"

    # ── SSRF guard for the NPCI connectivity probe ───────────────────
    # `npci_client._is_private_url` refuses to probe a URL that resolves into
    # private or reserved address space, so an operator cannot point
    # `npci_platform_url` at an internal service and use Settings → Test
    # Connection as a port scanner (SAST finding F-003).
    #
    # The guard originally had no escape hatch beyond three hardcoded host
    # names (`localhost`, `host.docker.internal`, `*_backend`), which made a
    # perfectly legitimate deployment unreachable: NPCI's own UAT platform sits
    # on RFC-1918 space, so every probe against it was refused before a single
    # packet left the container. The two settings below are the auditable way
    # to say "this private target is approved", mirroring
    # `ssrf_allowed_internal_hosts` / `ssrf_allow_private_networks` in the NPCI
    # backend's `core/ssrf_guard.py`.
    #
    # Both default to today's behaviour — nothing is loosened unless a
    # deployment opts in explicitly.
    #
    # Name the approved hosts. Comma-separated, matched on the URL host
    # (hostname or IP literal), case-insensitively:
    #   NPCI_SSRF_ALLOWED_HOSTS=10.84.12.34,npci-uat.internal
    npci_ssrf_allowed_hosts: str = ""
    # Blanket permission for RFC-1918 / unique-local space, for a deployment
    # where enumerating hosts is impractical. Prefer the allowlist above: this
    # opens the whole private range. Loopback and link-local stay blocked
    # either way — the cloud metadata endpoint (169.254.169.254) is never a
    # legitimate NPCI platform, so this flag does NOT re-enable it.
    npci_ssrf_allow_private_networks: bool = False

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
    ainxt_base_url: str = ""   # no default: set AINXT_BASE_URL to your gateway
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

    # ── Hostility-tier limits (security_architecture_skills.md §4) ───────────
    # Centralized in core/hostility.py — every value here is the single source
    # of truth for its boundary's size/rate/timeout/bulkhead configuration.
    # See docs/adr/ADR-0004-hostility-tier-registry.md.
    a2a_max_request_body_bytes: int = 10 * 1024 * 1024   # 10 MB — H3 ingress cap
    a2a_rate_limit_rps: int = 20                          # global A2A ingress sliding window
    a2a_inbound_max_concurrent: int = 50
    npci_outbound_timeout_s: float = 30.0
    npci_cb_failure_threshold: int = 5
    npci_cb_cooldown_s: float = 30.0
    npci_outbound_max_concurrent: int = 10
    llm_read_timeout_s: float = 300.0
    llm_cb_failure_threshold: int = 5
    llm_cb_cooldown_s: float = 30.0
    llm_max_concurrent_calls: int = 8
    agentic_max_concurrent_runs: int = 5

    # Dev-only escape hatch: when true, the A2A ingress accepts unauthenticated
    # calls if npci_jwt_secret/npci_hmac_secret are not yet configured. Defaults
    # to false (fail-closed). NEVER set this in a production or staging
    # deployment — see docs/adr/ADR-0003-fail-closed-a2a-ingress.md.
    partner_allow_unauthenticated_a2a: bool = False

    # Per-change LLM token budget (security_architecture_skills.md §4.2 —
    # cost is a bounded resource). 0 = unlimited (explicit opt-out).
    llm_token_budget_per_change: int = 2_000_000

    # ── Database connection pool (EA_Skills.md P6/P10 — externalize
    # infrastructure values instead of hardcoding them) ──────────────────────
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout_s: int = 30
    db_pool_recycle_s: int = 3600   # 1 hour — refresh before a managed DB's idle reaper drops it

    # ── Data retention / purge policy (security_architecture_skills.md §10.3,
    # EA_Skills.md P6 — TTL, validity, archival rules). 0 = disable that
    # sweep entirely (explicit opt-out). See services/retention.py. ─────────
    retention_keep_latest_iterations: int = 3       # GeneratedCodeFile iterations kept per change
    retention_agent_run_payload_days: int = 90      # AgentRun.result_payload cleared past this age
    retention_sweep_interval_s: int = 24 * 3600      # background sweep cadence

    # ── Outbound A2A retry sweep (Finding 12 — DLQ for partner->NPCI sends) ──
    # See services/outbound_retry.py / outbound_retry_scheduler.py.
    outbound_retry_sweep_interval_s: int = 60         # check for due retries every minute
    outbound_retry_max_attempts: int = 6              # -> abandoned after this many failed attempts

    # ── EA_Skills.md P2 (bounded structures) / P3 (safe scale-in) ────────────
    # Cap on the in-process revision-context cache. Bounded so a long-running
    # deployment processing many changes cannot accumulate every change's
    # assembled document set in RAM (agents/revision_context.py).
    context_cache_max_entries: int = 128

    # Graceful drain window. On shutdown the platform stops accepting new agent
    # jobs and waits up to this many seconds for in-flight ones to finish before
    # marking stragglers interrupted (P3: "safe scale-in with drain/linger
    # behavior"). Keep <= the orchestrator's termination grace period (Docker's
    # default `stop_grace_period` and Kubernetes' default
    # terminationGracePeriodSeconds are both 30s) — a longer value here would be
    # cut short by SIGKILL and the drain would not complete. 0 disables draining.
    shutdown_drain_timeout_s: int = 30

    # Cross-change agent-job concurrency cap (the `agent_job_dispatch` boundary
    # in core/hostility.py). Distinct from the per-change duplicate-dispatch 409
    # and the per-change token budget: this bounds how many agent jobs run at
    # once across DIFFERENT changes, so N simultaneous operators cannot exhaust
    # the worker pool. See api/dashboard/jobs.py.
    agent_job_bulkhead_timeout_s: float = 5.0

    # In-process rate limiting and caching are PER-PROCESS. Running multiple
    # workers/replicas silently multiplies the effective A2A rate limit (each
    # process enforces its own window) and splits the cache. Startup refuses a
    # multi-worker boot unless this is explicitly set — see main.py's
    # `_validate_single_instance_assumptions()` and the Redis upgrade path in
    # a2a_common/rate_limit_middleware.py.
    partner_allow_multi_worker: bool = False

    # Shared rate-limit backend. Empty (the default) keeps the per-process
    # window, which is correct for the documented single-instance deployment
    # and for the many partner forks that do not run redis. Set this to a
    # redis URL to make the A2A ingress limit SHARED across workers and
    # replicas — that is what makes a multi-worker boot safe, and
    # `core/runtime.validate_single_instance()` checks for it.
    # Requires the optional `redis` package; without it the limiter logs a
    # warning and stays per-process rather than failing to start.
    partner_rate_limit_redis_url: str = ""

    # SDLC Gap 7 (docs/ARCHITECTURE_REVIEW_ACTIONS.md, Tier 3): optional,
    # disabled-by-default test-generation agent (agents/test_files.py). Off by
    # default because it is explicitly supplementary — see ARCHITECTURE.md's
    # "Scope of the automated code-review gate" for why the review/fix loop
    # itself does not generate or run tests, and why this opt-in step does
    # not change that scope statement.
    enable_test_generation: bool = False

    # Escape hatch for the cleartext-URL guard below. Declared as a real field
    # (rather than read only via os.getenv) because pydantic-settings forbids
    # extra inputs: with `PARTNER_ALLOW_HTTP=true` present in a `.env` file,
    # an undeclared name aborts startup with
    #   "partner_allow_http — Extra inputs are not permitted"
    # instead of suppressing the guard. That made the guard's own advice fatal
    # for native deployments, where `.env` is the documented way to configure
    # the service (§4.3) — the setting only ever worked as a shell variable,
    # which nothing said. Declaring it means both forms behave identically.
    #
    # Still read through os.getenv below so a bare `PARTNER_ALLOW_HTTP=true`
    # exported in the environment keeps working exactly as before.
    partner_allow_http: bool = False

    # Key-encryption-key for `partner_settings` secrets (core/secret_box.py).
    # Declared for exactly the same reason as `partner_allow_http` above, and
    # it is the same bug: `.env` is the documented home for this value — this
    # file's own header (lines 10-16 of `.env`) says the KEK is one of three
    # credential lines appended there — but the name was only ever read via
    # `os.environ`, so putting it there aborted startup with
    #   "partner_secret_kek — Extra inputs are not permitted"
    # The operator's only working option was a shell export, which nothing
    # documented and which does not survive a restart from a fresh shell.
    # When that happens the failure is silent rather than loud: outbound calls
    # read every credential as "not configured" because
    # npci_client._get_setting swallows the decrypt error and returns the
    # default. That is the worst available failure mode for this value.
    #
    # NOT consumed here — secret_box reads it, and still prefers the real
    # environment variable so an exported KEK behaves exactly as before. This
    # field only makes the documented `.env` form stop crashing.
    partner_secret_kek: str = ""

    # ── Integration-testing tunnel, egress side (ITA I-1) ───────────────────
    # This platform receives an encapsulated HTTP exchange over A2A and makes
    # the call locally. OFF BY DEFAULT and dev-only: whoever reaches the far
    # ingress can otherwise make THIS platform issue requests to anything it
    # can reach, which is why the alias allowlist below — not the caller — is
    # what decides the target.
    integration_testing_enabled: bool = False
    # JSON object keyed by alias; validated at startup by
    # `validate_integration_testing_allowlist()`. A malformed policy stops the
    # app rather than starting it permissive.
    #   {"external_api": {"scheme": "http", "host": "api.internal",
    #                     "port": 8080, "path_prefixes": ["/v1/"]}}
    integration_testing_allowlist: str = ""
    # The innermost layer of the shrink-inward budget (ITA §6). The far side
    # sends the remaining budget as `deadline_ms`; the egress takes the smaller
    # of the two so a slow target cannot outlive the caller's window.
    integration_testing_target_timeout_s: float = 60.0
    integration_testing_max_body_bytes: int = 5 * 1024 * 1024
    integration_testing_max_hops: int = 1
    # ITA-4 (reverse direction): the outer two layers of the same shrink-inward
    # budget, mirroring the NPCI side's 105 → 90 → 60. `ingress_timeout_s` is
    # what this platform's own ingress route budgets end-to-end;
    # `a2a_timeout_s` is what the A2A send is given — it must exceed the far
    # side's 60s target ceiling or every slow case dies as a transport error
    # instead of the real `target_timeout`.
    integration_testing_ingress_timeout_s: float = 105.0
    integration_testing_a2a_timeout_s: float = 90.0
    # ITA-5 per-alias egress gates: a bulkhead so one saturated target cannot
    # monopolise the tunnel, and a circuit breaker so a dead target is refused
    # fast (code `circuit_open`) instead of burning each caller's full budget.
    integration_testing_max_concurrent_per_alias: int = 4
    integration_testing_breaker_failure_threshold: int = 5
    integration_testing_breaker_cooldown_s: float = 30.0
    # ITA I-8 (§3.5 Stage 2): emit a conforming `__cert/v1/trigger` handler
    # beside the generated API. OFF by default — with the flag off nothing is
    # emitted and Stage 1's hand-supplied trigger URL keeps working, which is
    # the whole point of the two stages sharing one contract.
    cert_emit_trigger_handler: bool = False

    # ── HMAC key-strength enforcement (CVE-2025-45768 hardening) ────────────
    # PyJWT 2.13.0 DOES detect an under-length HMAC key — it emits
    # `InsecureKeyLengthWarning` citing RFC 7518 §3.2 — but a warning does not
    # stop a process, and PyJWT can only see a key at signing time, which for
    # the two NPCI-issued secrets is already on the inbound request path. It
    # also cannot judge guessability: `"a" * 32` clears its length check.
    #
    # So this application escalates the warning to a hard failure, moves the
    # check to the point where a secret is INSTALLED, and adds the structural
    # rules PyJWT has no basis to apply. `app/core/key_strength.py` holds the
    # rules; this flag controls how hard they bite.
    #
    # No PyJWT upgrade clears the SBOM finding: the advisory attaches to the
    # component coordinates, and no release removes the ability to pass a weak
    # string. See security/vex/partner-platform.vex.json.
    #
    # WHY THIS FLAG EXISTS — a hard length floor is the one change in this
    # remediation that can turn a currently-working deployment into a failed
    # startup. Any environment already running a short secret (a partner's
    # on-prem install, a staging box seeded by hand) would stop booting the
    # moment it upgraded. That is a bad way to ship a security control: the
    # operator's incentive becomes "roll back", not "fix the secret".
    #
    # So the rollout is two-stage, and this is the switch:
    #   enforce_hmac_key_strength=false → weak secrets log a loud error and
    #                                     the service starts (observation window)
    #   enforce_hmac_key_strength=true  → weak secrets are fatal
    #
    # Production defaults to fatal regardless (see the guard below) because a
    # production deployment with a guessable signing key is the exact condition
    # the CVE describes. This flag only governs non-production environments,
    # where it defaults to warn so developers and CI are not blocked.
    # Override via ENFORCE_HMAC_KEY_STRENGTH.
    enforce_hmac_key_strength: bool = False


settings = Settings()

# ── Fail-fast: refuse to start with an unset database URL ──
if not settings.database_url:
    raise RuntimeError(
        "DATABASE_URL is unset. "
        "Set it to a valid Postgres connection string before starting the service. "
        "For docker-compose deployments, it is interpolated from PARTNER_POSTGRES_* "
        "variables in docker-compose.yml."
    )

def _env_is_protected() -> bool:
    """True for every environment except development.

    Read defensively — `app_env` comes from the environment and can carry
    whitespace or an unexpected type, and an unrecognised value must fail
    safe (treated as protected) rather than unlock a dev-only escape hatch.
    """
    return (str(getattr(settings, "app_env", "") or "")).strip().lower() != "development"


# ── Fail-fast: refuse to start production with an unset JWT secret ──
if not settings.session_jwt_secret:
    if settings.app_env == "production":
        raise RuntimeError(
            "SESSION_JWT_SECRET is unset. "
            "Set a strong, per-deployment value before running in production."
        )
    # Non-production with NO secret at all. Note what actually protects this
    # path: PyJWT raises `InvalidKeyError("HMAC key must not be empty.")` on the
    # first attempt to sign, so a session cannot silently be minted with an
    # empty key — the failure is loud and immediate rather than forgeable.
    # Verified against the pinned PyJWT 2.13.0.
    _config_logger.warning(
        "SESSION_JWT_SECRET is unset — login will fail until it is set "
        "(PyJWT rejects an empty HMAC key). "
        "Set a strong value via the SESSION_JWT_SECRET env var."
    )
else:
    # ── HMAC key STRENGTH, not just presence (CVE-2025-45768) ───────────────
    # The check above only asks "is a secret set?". That is the gap the CVE
    # names: `SESSION_JWT_SECRET=abc` passes an emptiness test and produces a
    # forgeable HS256 session token.
    #
    # PyJWT 2.13.0 would emit `InsecureKeyLengthWarning` for that value, but a
    # warning does not stop startup and is routinely filtered — and PyJWT has no
    # basis to reject `"a" * 32`, which clears its length check while remaining
    # trivially guessable. This block turns the same condition into a hard
    # failure and extends it to guessability.
    #
    # Only reached when a secret IS set, so it composes with the branch above
    # rather than duplicating the unset case.
    from app.core.key_strength import assess_hmac_secret, generation_hint

    _weak = assess_hmac_secret(
        settings.session_jwt_secret, label="SESSION_JWT_SECRET"
    )
    if _weak:
        _detail = "\n".join(f"  - {r}" for r in _weak)
        # Production is always fatal: a live deployment signing operator
        # sessions with a guessable key is precisely the CVE's condition, and
        # there is no legitimate reason for it. The opt-in flag cannot loosen
        # this — it only chooses the behaviour for non-production.
        if settings.app_env == "production" or settings.enforce_hmac_key_strength:
            raise RuntimeError(
                "SESSION_JWT_SECRET does not meet HMAC key-strength policy "
                "(CVE-2025-45768 hardening):\n"
                f"{_detail}\n"
                f"{generation_hint('SESSION_JWT_SECRET')}"
            )
        # Non-production during the rollout window: loud, actionable, non-fatal.
        # Logged at ERROR (not WARNING) so it surfaces in a default log level
        # and in any alerting that watches for errors — a warning here would be
        # lost in normal startup chatter.
        _config_logger.error(
            "SESSION_JWT_SECRET is WEAK and would be REJECTED in production "
            "(CVE-2025-45768 hardening):\n%s\n%s\n"
            "Set ENFORCE_HMAC_KEY_STRENGTH=true to make this fatal here too.",
            _detail,
            generation_hint("SESSION_JWT_SECRET"),
        )

# ── AR-13: refuse to start with the auth bypass outside development ──
# PARTNER_ALLOW_UNAUTHENTICATED_A2A makes the A2A ingress accept unsigned,
# unauthenticated calls when the NPCI secrets are not configured. It is a
# convenience for a fresh local checkout, and the field's own comment already
# said "NEVER set this in a production or staging deployment" — but nothing
# enforced it, so the only thing standing between a copied .env and an open
# A2A boundary was that sentence.
#
# Staging counts as protected, not just production: a staging stack is
# routinely reachable from the same networks as production and holds
# realistic data. Anything not clearly "development" is treated as protected,
# so APP_ENV=prod or a stray-whitespace value fails safe instead of quietly
# enabling the bypass.
#
# core/security_events.allow_unconfigured_bypass() enforces the same rule at
# request time; this block is the loud half, so the failure lands at deploy
# time rather than on the first unauthenticated call.
if settings.partner_allow_unauthenticated_a2a and _env_is_protected():
    raise RuntimeError(
        f"PARTNER_ALLOW_UNAUTHENTICATED_A2A=true with APP_ENV={settings.app_env!r}. "
        "This accepts unauthenticated A2A calls whenever NPCI_JWT_SECRET or "
        "NPCI_HMAC_SECRET is unset, which removes the authentication boundary "
        "entirely. It is a development-only escape hatch.\n"
        "  * Deploying for real: unset PARTNER_ALLOW_UNAUTHENTICATED_A2A and "
        "configure the NPCI secrets in partner_settings.\n"
        "  * Local development: set APP_ENV=development.\n"
        "See docs/adr/ADR-0003-fail-closed-a2a-ingress.md."
    )

# ── Fail-fast on cleartext service URLs in any environment ──
# npci_platform_url, partner_public_url and ollama_url all default to
# http:// for local/docker-compose convenience. partner_public_url in
# particular is the address the A2A SDK posts JSON-RPC bodies (certification
# envelopes, task payloads) to — an unchanged http:// default sends that
# traffic in the clear. This guard raises a hard error in ALL environments
# unless PARTNER_ALLOW_HTTP=true is explicitly set, so staging/QA deployments
# don't silently transmit real credentials over cleartext (SAST finding F-001).
# Honour EITHER source: the shell environment (original behaviour) or the
# declared setting, which is what picks the value up from a `.env` file.
_ALLOW_HTTP = (
    os.getenv("PARTNER_ALLOW_HTTP", "").strip().lower() == "true"
    or settings.partner_allow_http
)
_http_defaults = {
    "npci_platform_url": settings.npci_platform_url,
    "partner_public_url": settings.partner_public_url,
    "ollama_url": settings.ollama_url,
}
_cleartext = {k: v for k, v in _http_defaults.items() if (v or "").lower().startswith("http://")}
if _cleartext:
    detail = ", ".join(f"{k}={v!r}" for k, v in _cleartext.items())
    if _ALLOW_HTTP:
        _config_logger.warning(
            "The following URL(s) use http:// (cleartext): %s. "
            "PARTNER_ALLOW_HTTP=true is set — proceeding insecurely. "
            "Set the corresponding env var(s) to https:// for any deployment "
            "that carries real credentials or sensitive data.",
            detail,
        )
    else:
        raise RuntimeError(
            f"The following URL(s) use http:// (cleartext): {detail}. "
            f"Set the corresponding env var(s) to https:// before running. "
            f"For local development only, set PARTNER_ALLOW_HTTP=true to "
            f"suppress this error."
        )

# ── Fail-fast on a malformed integration-testing allowlist (ITA §2) ──
# The tunnel makes THIS platform issue HTTP requests on someone else's behalf,
# so the alias allowlist is its command allowlist. Security skill §4.3:
# validate tier config at boot. A tunnel that starts with an unparsable policy
# and decides per request is one bad `except` away from failing OPEN, which
# here means arbitrary SSRF — refusing to boot is the safe failure.
if settings.integration_testing_enabled:
    from app.a2a_common.integration_allowlist import (  # noqa: E402
        AllowlistError as _AllowlistError, load_allowlist as _load_allowlist,
    )

    try:
        _load_allowlist(settings.integration_testing_allowlist)
    except _AllowlistError as _exc:
        raise RuntimeError(
            f"INTEGRATION_TESTING_ALLOWLIST is invalid: {_exc}. The tunnel "
            f"refuses to start with a policy it cannot parse rather than run "
            f"permissive."
        ) from None

    # ITA-5: the §6 budget must SHRINK INWARD (ingress > A2A send >
    # egress→target). Equal-or-inverted layers only misbehave under load — the
    # outermost fires first and every failure reads as a generic 504 with no
    # inner detail — so the ordering is refused at boot.
    if not (settings.integration_testing_ingress_timeout_s
            > settings.integration_testing_a2a_timeout_s
            > settings.integration_testing_target_timeout_s):
        raise RuntimeError(
            "integration-testing timeout budget must shrink inward: "
            f"ingress ({settings.integration_testing_ingress_timeout_s}s) > "
            f"a2a ({settings.integration_testing_a2a_timeout_s}s) > "
            f"target ({settings.integration_testing_target_timeout_s}s). "
            "See INTEGRATION_TESTING_AGENT_PLAN.md §6."
        )
