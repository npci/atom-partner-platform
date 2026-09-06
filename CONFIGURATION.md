# Configuration Guide — Partner Platform

Where every setting lives, which surface wins when two disagree, and what the
platform refuses to start without.

> **Verified at:** commit `e788d7b`, 2026-09-03. Fields counted in
> `backend/app/config.py`; runtime keys in `backend/app/core/setting_keys.py`;
> startup guards read from `config.py`.

For installing the stack see [`DEPLOYMENT_GUIDE.md`](DEPLOYMENT_GUIDE.md); for
the agent contract see [`ARCHITECTURE.md`](ARCHITECTURE.md); for what the
screens do see [`USER_GUIDE.md`](USER_GUIDE.md).

- [Two surfaces, not one](#two-surfaces-not-one)
- [Secrets at rest](#secrets-at-rest)
- [What stops the platform starting](#what-stops-the-platform-starting)
- [Settings by group](#settings-by-group)
- [Local development](#local-development)
- [Conventions that fail silently](#conventions-that-fail-silently)

---

## Two surfaces, not one

Configuration lives in two places with different lifetimes, and almost every
setting belongs to exactly one of them.

| Surface | What it is | Count | Changing it |
|---|---|---|---|
| **Environment** | `Settings` in `app/config.py`, read from real env vars and `backend/.env` at import | **73 fields** | Requires a restart |
| **`partner_settings` table** | Rows edited through the **Settings** screen, encrypted at rest | **7 keys** | Takes effect without a restart |

The seven runtime keys are:

```
npci_jwt_secret          npci_hmac_secret        partner_api_key
partner_anthropic_api_key    gitlab_token
npci_platform_url        partner_name
```

### Which wins

**They are mostly not layered — each setting has one home.** The Authority
credentials are a good example: `npci_jwt_secret` and `npci_hmac_secret` are
read *only* from `partner_settings`, decrypted at use. There is no environment
fallback, and an absent row is not "unconfigured, carry on" — the A2A ingress
answers `503` and refuses the call. See
[`wiki/security-layers.md`](wiki/security-layers.md).

Two settings do overlap, and in both the **runtime value wins**:

- **LLM keys.** An agent receives a key from the caller (sourced from
  `partner_settings`) and uses it; only when that is empty does it fall back to
  the environment field for the active provider. So a key set in the Settings
  screen overrides `PARTNER_ANTHROPIC_API_KEY`, and with neither set the agents
  return labelled mock output rather than failing.
- **`PARTNER_SECRET_KEK`.** Here it is the reverse and deliberately so — a real
  environment variable beats the `.env`-declared field, because the KEK is
  meant to arrive from a secret manager at deploy time.

---

## Secrets at rest

`core/secret_box.py` performs envelope encryption on every secret value in
`partner_settings`. The key-encryption-key comes from **`PARTNER_SECRET_KEK`**.

**Set it before storing any secret.** Without it the platform refuses to write a
secret rather than storing it in the clear — a refusal, not a warning.

`decrypt()` reads three historical formats, so an upgrade over an existing
volume does not strand rows written before encryption landed. That tolerance is
one-directional: new writes are always encrypted.

**Rotating the KEK is not a config edit.** Values encrypted under the old key
cannot be read under the new one. Re-enter the secrets through the Settings
screen after rotating, or decrypt-and-rewrite them first.

---

## What stops the platform starting

Seven guards raise at import rather than letting a misconfigured process serve
traffic. This is the list to check first when the container will not come up.

| Guard | Fires when |
|---|---|
| `DATABASE_URL` unset | No Postgres connection string |
| `SESSION_JWT_SECRET` unset | No signing secret; sessions would be forgeable |
| `SESSION_JWT_SECRET` too weak | Fails HMAC key-strength policy (CVE-2025-45768 hardening) |
| `PARTNER_ALLOW_UNAUTHENTICATED_A2A=true` outside development | The escape hatch is inert in a protected environment — setting it stops the platform rather than weakening it |
| A configured URL uses `http://` | Cleartext to a non-local destination, unless `PARTNER_ALLOW_HTTP=true` |
| `INTEGRATION_TESTING_ALLOWLIST` unparsable | The tunnel refuses to start with a policy it cannot read rather than running permissive |
| Tunnel timeout budget not shrinking inward | An inner timeout longer than its outer one, which would strand requests |

The last two are worth reading twice. Both exist because the failure they
prevent is silent: a tunnel that boots with an unreadable allowlist and then
fails open is worse than one that does not boot.

---

## Settings by group

73 environment fields. The groups that matter most:

| Group | Fields | Notable |
|---|---|---|
| Partner identity & secrets | 13 | `partner_name`, `partner_public_url`, per-provider API keys |
| Integration-testing tunnel | 10 | `integration_testing_enabled` (off by default), `integration_testing_allowlist`, three timeout budgets |
| Authority link | 7 | `npci_platform_url`, `npci_ssrf_allowed_hosts`, `npci_ssrf_allow_private_networks` |
| LLM | 6 | `llm_provider`, read timeout, circuit-breaker threshold |
| Models & embeddings | 10 | `claude_model`, `openai_model`, `ainxt_*`, `embed_model`, `embed_dim` |
| Database pool | 4 | `db_pool_size`, `db_pool_recycle_s` — externalised rather than hardcoded |
| Retention | 3 | How long generated code iterations and agent payloads are kept |
| Outbound retry | 2 | Sweep interval and `outbound_retry_max_attempts` (6, then abandoned) |

The complete annotated list is [`backend/.env.example`](backend/.env.example) —
33 documented variables across 171 lines, most of them explained inline. It
covers the settings a deployment actually changes, not all 73 fields: anything
absent from it keeps the default in `app/config.py`. Copy it to
`backend/.env` before first run.

---

## Local development

The flags most often changed for local work:

| Variable | Effect |
|---|---|
| `APP_ENV=development` | Relaxes the production-only guards |
| `PARTNER_ALLOW_HTTP=true` | Permits `http://` targets for a local compose stack |
| `INTEGRATION_TESTING_ENABLED` | Off by default; production config refuses it |
| `ENABLE_TEST_GENERATION` | Off by default — see the review-gate scope in [`ARCHITECTURE.md`](ARCHITECTURE.md) |
| `ENFORCE_HMAC_KEY_STRENGTH` | Off by default; turn it on to fail rather than warn on a weak secret |

Running with no LLM key at all is a supported path: every agent returns
documented, clearly-labelled mock output, so a fresh clone produces a complete
flow without credentials.

---

## Conventions that fail silently

**An undeclared environment variable is ignored, not rejected.** If a name is
not a field on `Settings`, setting it does nothing and the default stands. A
typo in a variable name is therefore invisible — no warning, no error, just the
old behaviour. Check the spelling against `app/config.py` before concluding a
setting has no effect.

**Environment settings are read at import.** Changing `backend/.env` does
nothing until the container restarts. Only the seven `partner_settings` keys
apply live.

**The tunnel's allowlist is not a URL filter.** Callers supply an *alias*, which
the receiving side resolves against its own allowlist. There is no setting that
makes it accept a caller-supplied URL, and that is the point — see
[`wiki/security-layers.md`](wiki/security-layers.md).

---

## Related documents

| Document | Covers |
|---|---|
| [`DEPLOYMENT_GUIDE.md`](DEPLOYMENT_GUIDE.md) | Installing and running, Docker and native |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | The agent contract, bindings, prompts |
| [`USER_GUIDE.md`](USER_GUIDE.md) | The platform from the operator's chair |
| [`wiki/security-layers.md`](wiki/security-layers.md) | What each credential actually guards |
| [`backend/.env.example`](backend/.env.example) | Every variable, annotated |
