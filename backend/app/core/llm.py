"""Unified LLM abstraction for the partner platform.

Mirrors `backend/app/core/llm.py` on the NPCI side, but trimmed to the
partner stack's actual surface today:

  * Synchronous `call_llm` only — partner has no streaming call sites.
  * Three providers (claude / openai / ainxt) — drops Ollama (partner
    stacks don't run a local model).
  * No retry loop, no AiNxt placeholder-error recovery, no
    context-budget assertion, no observability ContextVar plumbing.
    Add those by lifting from the NPCI module when partner-side LLM
    use grows enough to justify the surface.

Provider switching is configured via env (mirroring the NPCI shape):

  LLM_PROVIDER=claude|openai|ainxt
  PARTNER_ANTHROPIC_API_KEY=...     # for claude
  PARTNER_OPENAI_API_KEY=...        # for openai
  PARTNER_AINXT_API_KEY=...         # for ainxt
  AINXT_BASE_URL=https://ainxt.npci.org.in/ainxt/v1/api    # default
  CLAUDE_MODEL / OPENAI_MODEL / AINXT_MODEL                # per-provider
  AINXT_COMPAT_MODE=openai|anthropic   # AiNxt wire: /chat/completions or /v1/messages
  AINXT_MESSAGES_MODEL=...             # model when AINXT_COMPAT_MODE=anthropic

Public surface:
  call_llm(system, messages, *, max_tokens=4000, model=None, provider=None) -> str
  get_provider() -> str
  get_model(provider=None) -> str
"""
from __future__ import annotations

import logging
from functools import lru_cache

from app.config import settings

logger = logging.getLogger(__name__)


def get_provider() -> str:
    """Active provider, lowercased, defaulting to claude."""
    return (settings.llm_provider or "claude").lower().strip()


def _ainxt_uses_anthropic() -> bool:
    """True when AiNxt should use the Anthropic /v1/messages route instead of the
    OpenAI /chat/completions route."""
    return (getattr(settings, "ainxt_compat_mode", "") or "").strip().lower() == "anthropic"


def get_model(provider: str | None = None) -> str:
    """Configured model name for `provider` (or the active default)."""
    p = (provider or get_provider()).lower().strip()
    if p == "openai":
        return settings.openai_model or "gpt-4o-mini"
    if p == "ainxt":
        # The /v1/messages route wants an Anthropic-style model id.
        if _ainxt_uses_anthropic():
            return settings.ainxt_messages_model or "claude-sonnet-4-6"
        return settings.ainxt_model or "gpt-4o"
    return settings.claude_model or "claude-sonnet-4-6"


# ── Provider clients (cached; built lazily so startup doesn't fail
#    when the configured provider's key is empty for a partner that
#    doesn't use that provider) ─────────────────────────────────────


@lru_cache(maxsize=1)
def _get_anthropic_client():
    from anthropic import Anthropic
    api_key = settings.partner_anthropic_api_key
    if not api_key:
        raise RuntimeError(
            "PARTNER_ANTHROPIC_API_KEY not set — required when LLM_PROVIDER=claude. "
            "Either set the env var or change LLM_PROVIDER."
        )
    return Anthropic(api_key=api_key)


@lru_cache(maxsize=1)
def _get_openai_client():
    from openai import OpenAI
    api_key = settings.partner_openai_api_key
    if not api_key:
        raise RuntimeError(
            "PARTNER_OPENAI_API_KEY not set — required when LLM_PROVIDER=openai."
        )
    return OpenAI(api_key=api_key)


@lru_cache(maxsize=1)
def _get_ainxt_client():
    from openai import OpenAI
    api_key = settings.partner_ainxt_api_key
    base_url = settings.ainxt_base_url
    if not api_key:
        raise RuntimeError(
            "PARTNER_AINXT_API_KEY not set — required when LLM_PROVIDER=ainxt."
        )
    return OpenAI(api_key=api_key, base_url=base_url)


@lru_cache(maxsize=1)
def _get_ainxt_anthropic_client():
    """AiNxt over the Anthropic /v1/messages route (ainxt_compat_mode=anthropic).

    The Anthropic SDK is pointed at ainxt_base_url and appends /v1/messages. AiNxt
    authenticates that route via `Authorization: Bearer`, so the key goes in
    `auth_token=` (NOT `api_key=`, which sends `x-api-key` and 401s here)."""
    from anthropic import Anthropic
    api_key = settings.partner_ainxt_api_key
    if not api_key:
        raise RuntimeError(
            "PARTNER_AINXT_API_KEY not set — required when LLM_PROVIDER=ainxt."
        )
    return Anthropic(auth_token=api_key, base_url=settings.ainxt_base_url)


def reset_clients_for_tests() -> None:
    """Test hook — clear cached clients so credential changes re-load."""
    _get_anthropic_client.cache_clear()
    _get_openai_client.cache_clear()
    _get_ainxt_client.cache_clear()
    _get_ainxt_anthropic_client.cache_clear()


# ── Public entry point ───────────────────────────────────────────────────────


def call_llm(
    system: str,
    messages: list[dict],
    *,
    max_tokens: int = 4000,
    model: str | None = None,
    provider: str | None = None,
    api_key: str | None = None,
) -> str:
    """Synchronous LLM call. Returns the response text.

    Raises on failure — the caller decides whether to recover. Existing
    partner-side callers (`question_suggester`) already wrap this in a
    try/except and fall back to an empty result, which is the right
    behaviour for "no AI available, render the page anyway".

    `api_key` overrides the env-level key for THIS call only, building
    a one-shot un-cached client. Supports the "user pastes key in
    Settings UI" flow that stores secrets in `partner_settings` and
    threads them through at request time. Pass None to use the cached
    client built from env at startup.
    """
    p = (provider or get_provider()).lower().strip()
    chosen_model = model or get_model(provider=p)

    logger.info(
        "LLM call: provider=%s model=%s messages=%d max_tokens=%d override_key=%s",
        p, chosen_model, len(messages), max_tokens, bool(api_key),
    )

    if p == "claude":
        client = _build_anthropic(api_key) if api_key else _get_anthropic_client()
        return _call_claude(client, system, messages, chosen_model, max_tokens)
    if p == "openai":
        client = _build_openai(api_key) if api_key else _get_openai_client()
        return _call_openai_compat(client, system, messages, chosen_model, max_tokens)
    if p == "ainxt":
        # AiNxt speaks two wires: the Anthropic /v1/messages route (native tool_use,
        # stop_reason) or the OpenAI /chat/completions route. Toggle: ainxt_compat_mode.
        if _ainxt_uses_anthropic():
            client = _build_ainxt_anthropic(api_key) if api_key else _get_ainxt_anthropic_client()
            return _call_claude(client, system, messages, chosen_model, max_tokens)
        client = _build_ainxt(api_key) if api_key else _get_ainxt_client()
        return _call_openai_compat(client, system, messages, chosen_model, max_tokens)
    raise ValueError(
        f"Unknown LLM provider: {p!r}. Set LLM_PROVIDER to one of: claude, openai, ainxt."
    )


# ── One-shot client builders for runtime key overrides ───────────────────────


def _build_anthropic(api_key: str):
    from anthropic import Anthropic
    return Anthropic(api_key=api_key)


def _build_openai(api_key: str):
    from openai import OpenAI
    return OpenAI(api_key=api_key)


def _build_ainxt(api_key: str):
    from openai import OpenAI
    return OpenAI(api_key=api_key, base_url=settings.ainxt_base_url)


def _build_ainxt_anthropic(api_key: str):
    # Runtime key-override twin of _get_ainxt_anthropic_client — Bearer auth (auth_token).
    from anthropic import Anthropic
    return Anthropic(auth_token=api_key, base_url=settings.ainxt_base_url)


# ── Provider-specific helpers ────────────────────────────────────────────────


def _call_claude(client, system: str, messages: list[dict], model: str, max_tokens: int) -> str:
    # Streaming, accumulated to a single response. Required once max_tokens is
    # large: the SDK rejects non-streaming requests it estimates may exceed its
    # 10-minute ceiling, and the document-bearing agents (design/code/test) need
    # 30k+ token budgets.
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
    ) as stream:
        resp = stream.get_final_message()
    # The agents parse this text as JSON — a max_tokens truncation yields an
    # unparseable half-document, so fail loudly with the real cause instead of
    # letting the caller log "no valid JSON".
    if resp.stop_reason == "max_tokens":
        raise RuntimeError(
            f"LLM output truncated at max_tokens={max_tokens} (model={model}) — "
            f"raise the caller's max_tokens budget."
        )
    # Anthropic returns a list of content blocks; concatenate the text ones.
    parts: list[str] = []
    for block in resp.content or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    text = "".join(parts).strip()
    if not text:
        # Either the model returned tool-use blocks only (we don't use
        # tools here) or stopped without content. Surface empty rather
        # than a misleading IndexError on resp.content[0].text.
        logger.warning("Claude returned no text blocks for model=%s", model)
    return text


def _call_openai_compat(client, system: str, messages: list[dict], model: str, max_tokens: int) -> str:
    """OpenAI-compatible providers share the same SDK + wire shape; only the
    base_url and auth key differ, which the cached client already encapsulates."""
    oai_messages = [{"role": "system", "content": system}, *messages]
    resp = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=oai_messages,
    )
    # Conforming providers (OpenAI, Azure OpenAI, …) return a completion object.
    # Some OpenAI-compatible gateways/proxies, however, answer a non-streaming
    # request with a raw SSE text/event-stream body, so the SDK hands back a
    # plain *string* — and `resp.choices` then raises "'str' object has no
    # attribute 'choices'". Recover the assistant text from the stream lines
    # rather than crashing; this path is inert for conforming providers.
    if isinstance(resp, str):
        return _recover_text_from_stream_string(resp, model=model, max_tokens=max_tokens)
    if not resp.choices:
        raise RuntimeError(f"LLM returned empty choices: {resp}")
    choice = resp.choices[0]
    # Same truncation guard as the claude path — half a JSON document is worse
    # than a clear error.
    if getattr(choice, "finish_reason", None) == "length":
        raise RuntimeError(
            f"LLM output truncated at max_tokens={max_tokens} (model={model}) — "
            f"raise the caller's max_tokens budget."
        )
    return (choice.message.content or "").strip()


def _recover_text_from_stream_string(raw: str, *, model: str, max_tokens: int) -> str:
    """Extract assistant text when an OpenAI-compatible gateway returns a raw
    SSE string instead of a completion object (see `_call_openai_compat`).

    Handles both the OpenAI streaming shape (`choices[].delta.content`) and the
    Anthropic shape (`delta.text`); falls back to the raw string if nothing
    parses, so the caller still gets *something* rather than an exception. Raises
    on a detected truncation, matching the completion-object path — a half JSON
    document is worse than a clear error.
    """
    import json

    parts: list[str] = []
    truncated = False
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload in ("[DONE]", ""):
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        # OpenAI streaming format
        for ch in (obj.get("choices") or []):
            txt = (ch.get("delta") or {}).get("content") or \
                  (ch.get("message") or {}).get("content") or ""
            if txt:
                parts.append(txt)
            if ch.get("finish_reason") == "length":
                truncated = True
        # Anthropic streaming format
        delta = obj.get("delta") or {}
        if delta.get("type") == "text_delta":
            parts.append(delta.get("text", ""))
        if obj.get("type") == "message_delta" and \
                (obj.get("delta") or {}).get("stop_reason") == "max_tokens":
            truncated = True

    text = "".join(parts).strip() if parts else raw.strip()
    if truncated:
        raise RuntimeError(
            f"LLM output truncated at max_tokens={max_tokens} (model={model}) — "
            f"raise the caller's max_tokens budget."
        )
    return text
