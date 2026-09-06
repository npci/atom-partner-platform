# Frequently Asked Questions

Questions that come up when evaluating, forking, running or extending the
Partner Platform. For a symptom-and-fix table see the Troubleshooting section of
[`README.md`](README.md); for install and configuration detail see
[`DEPLOYMENT_GUIDE.md`](DEPLOYMENT_GUIDE.md). This document answers the *why*
questions those do not.

- [About the project](#about-the-project)
- [Forking and deploying](#forking-and-deploying)
- [The A2A boundary](#the-a2a-boundary)
- [Agents](#agents)
- [Security and data](#security-and-data)
- [Contributing](#contributing)

---

## About the project

### What is this, and who is it for?

The receiving side of a specification change. A central authority publishes a
change over Google's Agent-to-Agent protocol; this platform receives it,
routes it through your own agents, tracks implementation, negotiates rollout
terms, and drives certification back. It is a **reference implementation
intended to be forked** by the organisations that must implement changes.

### How does it relate to AtOM?

[AtOM](https://github.com/npci/atom-network-platform) is the
Authority side — it authors changes and distributes them. This platform is a
*client* of it over A2A, not a co-deployed service. They are separate
repositories with separate databases and trust each other only across the A2A
boundary.

### What am I expected to change after forking?

The agent tier. The architecture is three tiers — a contract tier that is shared
and must not diverge, a platform tier providing transport, persistence, audit
and admission, and an agent tier that is your extension zone. Agents cannot
replace platform admission, audit or authentication, which is what makes the
extension zone safe. See [`ARCHITECTURE.md`](ARCHITECTURE.md).



---

## Forking and deploying

### Do I need Redis?

No. Most partner stacks do not run it, and nothing requires it by default. It
becomes relevant only if you run **multiple workers or replicas**: the A2A rate
limiter keeps its counter per-process, so N workers would enforce N independent
windows. Setting `PARTNER_RATE_LIMIT_REDIS_URL` moves that counter to a shared
Redis window. The `redis` package is an optional dependency, listed commented-out
in `requirements.txt`, and `requirements.lock` is deliberately untouched so no
existing fork's build changes.

### Why does startup refuse when I set multiple workers?

Because the rate limit and the revision-context cache are per-process, so N
workers silently multiply the effective limit while the config still reads the
single-process number. There are two ways forward and the error message names
both: configure a shared limiter (the supported route — the check then passes on
its own, because nothing is weakened), or set `PARTNER_ALLOW_MULTI_WORKER=true`
to explicitly accept a multiplied limit.

### Why won't it start over an `http://` URL?

`npci_platform_url`, `partner_public_url` and `ollama_url` all default to
`http://` for local convenience, and `partner_public_url` in particular is where
A2A JSON-RPC bodies are posted. An unchanged default would send that traffic in
the clear. Set `PARTNER_ALLOW_HTTP=true` for development only.

### Do I need a TLS certificate to run locally?

The `edge` service refuses to start without `deploy/tls/{cert,key}.pem`.
[`DEPLOYMENT_GUIDE.md`](DEPLOYMENT_GUIDE.md) §3 generates a self-signed pair for
local use; replace it with a real certificate for anything beyond that.

### Can I run it without Docker?

Yes — [`DEPLOYMENT_GUIDE.md`](DEPLOYMENT_GUIDE.md) §4 covers a native install
including building pgvector from source and pulling the embedding model.

---

## The A2A boundary

### Inbound calls are rejected. How do I tell which layer refused?

Each layer fails differently and deliberately: a missing secret gives 503, an
invalid credential 401, a replayed or skewed envelope its own reason code, and a
flood 429. Structured security events are emitted for each, so the answer is in
the logs rather than in guesswork.

### Why did my clock cause a rejection?

Inbound HMAC envelopes carry a timestamp checked against a five-minute window.
Drift beyond it is rejected as `timestamp_skew`. Sync NTP on both sides.

### May I edit `a2a_common/`?

Three files — `hmac_signer.py`, `protocol.py`, `executor_base.py` — are mirrored
byte-for-byte with the Authority platform, because both sides hash the same wire
bytes. Neither repository's CI can see the other's copies, so a one-sided edit
leaves both test suites green while live calls are rejected. Changes there are a
coordinated release. The rest of `a2a_common/` is this platform's own.

### Is the auth bypass safe to use?

`PARTNER_ALLOW_UNAUTHENTICATED_A2A` exists for a fresh local checkout and
nothing else. It now refuses startup outside development and is ignored at
request time even if set. Staging counts as protected alongside production, and
an unrecognised `APP_ENV` fails safe rather than unlocking it.

---

## Agents

### How do I add my own agent?

Implement the agent contract and register it in `backend/config/agents.yaml`.
[`ARCHITECTURE.md`](ARCHITECTURE.md) has a build-your-own walkthrough, and
[`DEPLOYMENT_GUIDE.md`](DEPLOYMENT_GUIDE.md) §7 covers the framework.

### Do my agents run with the platform's credentials?

No. Partner capabilities, prompts and credentials stay in your environment. The
platform owns admission, correlation, audit, budget and cleanup around the
agent, not inside it — so an agent cannot bypass those controls, and every run
is recorded in `agent_runs`.

### Does the platform generate and run tests for me?

Test generation is off by default (`ENABLE_TEST_GENERATION`) and explicitly
supplementary. The automated review and fix loop does not generate or run tests
— [`ARCHITECTURE.md`](ARCHITECTURE.md) has a section stating the scope of that
gate plainly, so the guarantee is not overread.

---

## Security and data

### Where do I report a vulnerability?

Privately, per [`SECURITY.md`](SECURITY.md). Never in a public issue.

### How are stored secrets protected?

NPCI JWT and HMAC secrets, the partner API key, the LLM key and the GitLab token
are encrypted at rest in `partner_settings` under a key derived from
`PARTNER_SECRET_KEK`, which must be set before saving any secret. Encrypt and
decrypt raise a clear error rather than silently storing plaintext.

### Why does the retry queue show an exception type instead of a message?

Deliberately. `last_error` is rendered in the retry-queue view, and an httpx or
auth message would pin the resolved NPCI host, port and token prefix into a
user-visible row (CWE-209). The type name is enough to triage; the full detail
stays in the logs at DEBUG.

---

## Contributing

### What do I have to do before opening a pull request?

Sign off every commit (`git commit -s` — DCO, not a CLA), use Conventional
Commits, and run the tests and build. See [`CONTRIBUTING.md`](CONTRIBUTING.md).

### What breaks silently if I get it wrong?

Three things, and the pull-request template lists them: editing the mirrored
`a2a_common` wire files without a coordinated release; adding a database column
without an `_ensure_*()` helper (this platform has no Alembic — a new *table*
comes from `create_all`, but an added *column* does not); and swapping
`asyncio.run` for `anyio.from_thread` in a path reached from
`handlers/_background.py`, whose threads come from `asyncio.to_thread` and are
not anyio workers.

### The licence is MIT — what does that mean for the marks?

MIT contains no trademark clause, so it conveys no trademark rights. See
[`TRADEMARKS.md`](TRADEMARKS.md). This matters more for a forkable project than
most: you may state that your product is built on this software, but not use
NPCI's or any third party's marks in a way suggesting endorsement.
