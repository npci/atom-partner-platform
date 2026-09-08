# Security Policy

## Reporting a Vulnerability

**Do not open a public issue for a security problem.**

Report privately to **`atom.support@npci.org.in`**, with `[SECURITY]`
at the start of the subject line.

That address is the project's shared open-source inbox and is monitored by the
maintainers of both this repository and the Authority platform; the subject-line
tag is what routes it for triage ahead of general correspondence. One inbox
covers both because the A2A contract spans both, and a wire-protocol finding
usually affects each side.

---

## What to Include

A good vulnerability report includes:

- **Description** — what the vulnerability is and its potential impact.
- **Steps to reproduce** — a minimal, reliable reproduction path.
- **Affected component** — which service, file, or endpoint is affected.
- **Suggested fix** — optional, but appreciated.
- **Your contact details** — so we can keep you updated and credit you.

---

## Response Timeline

| Stage | Target |
|---|---|
| Acknowledgement | Within **48 hours** of receipt |
| Initial assessment | Within **5 business days** |
| Fix or mitigation | Within **30 days** for critical; **90 days** for others |
| Public disclosure | Coordinated with reporter after fix is released |

We follow a **coordinated disclosure** model. We will not take legal action
against researchers who report vulnerabilities in good faith and follow this
policy.

---

## Scope

### In Scope

- The partner backend (`backend/app/`), including the A2A wire code in
  `backend/app/a2a_common/` **as it exists in this repository**
- The agent framework and the shipped reference agents (`backend/app/agents/`)
- The frontend (`frontend/`)
- Authentication, session handling, and the settings/credential surface
- The GitLab integration (`backend/app/services/git_integrator.py`)
- Container and Compose configuration in this repository

### Out of Scope

- **The Authority platform.** It lives in its own repository — report against
  <https://github.com/npci/atom-network-platform> instead. Findings in the shared
  A2A code as it exists *here* remain in scope here, and we will coordinate the
  fix across both.
- **Your fork's agent bodies.** This is reference base code; the agents are
  meant to be replaced. Findings in the *framework* are in scope, findings in
  your own replacement logic are not.
- Vulnerabilities in third-party dependencies (report those upstream)
- Social engineering attacks
- Physical security
- Denial-of-service attacks that require significant resources

---

## Supported Versions

Pre-1.0. Only `main` receives fixes. There is no long-term-support branch and
no backporting.

---

## Threat Model — Read Before Deploying

This platform **receives documents from an external party, feeds them to a model
with tools, generates code, and drives git**. That is a larger blast radius than
a typical web application, and the honest posture matters more than a reassuring
one.

### Prompt injection is mitigated, not solved

Change communications, Product Kit documents, and retrieved code chunks are
**untrusted input that reaches a model**. They arrive over the A2A wire from the
Authority and are chunked into a retrieval corpus. Generated code is gated
behind a human-opened merge request.

None of that is a proof. A sufficiently clever document may still influence
generated output. **Treat every generated artifact as a proposal requiring human
review, never as an authority.**

### What is and is not automated

- `git_integrator.open_merge_request()` creates a branch, commits files, and
  opens a merge request. It **never merges** — that is enforced in code, and it
  should stay that way in your fork.
- Nothing is deployed by this platform.
- Feasibility assessments, progress reports and readiness declarations are sent
  to the Authority. Review before dispatch; a wrong readiness declaration is a
  business event, not just a bug.

### Deployment expectations

- **Never expose the backend directly.** Do not add a `ports:` publish to the
  backend service in `docker-compose.yml`; it is intentionally reachable only
  through the `edge` proxy, which terminates TLS.
- **Set a strong `ADMIN_PASSWORD` before first start.** The service refuses to
  seed an admin account without one — there is no default password. Keep the
  value out of version control and rotate it if it may have leaked.
- **Set `SESSION_JWT_SECRET` explicitly.** It has no safe default. Sessions
  signed with a guessable secret are forgeable.
- **Protect the Authority-issued credentials.** The partner API key and the
  JWT/HMAC secrets are entered through the Settings UI and stored per
  deployment. Anyone with admin access to this platform can act as your
  organisation on the A2A wire.
- **Give the platform the narrowest GitLab token that works.** A token that can
  push to a default branch is a token an injected prompt can try to use. It
  needs enough to create a branch and open a merge request, and no more.
- **Keep the CORS allowlist tight.** The allowlist is derived from the
  configured authority URL plus `PARTNER_CORS_EXTRA_ORIGINS`; a literal `*` in
  that variable is rejected at startup. List extra origins explicitly, and do
  not reintroduce a wildcard.

### Known gaps

We would rather tell you than have you find out:

- **Login lockout and the JWT denylist are in-memory and single-instance.**
  `backend/app/api/auth.py` keeps both in process dictionaries. Run more than
  one replica and a revoked token stays valid on the other replicas, and
  lockout counters are per-process. There is no CAPTCHA on login — the tiered
  lockout (5 failures → 60s, 10 → 5 min) is the only brute-force control.
- **Nonce replay protection is active, backed by the database.**
  `hmac_middleware.py` wires a nonce store into `hmac_signer.verify()` via
  `a2a_common/nonce_store_db.py`: Redis-backed where a Redis is deployed,
  Postgres-backed otherwise. The store fails closed — if it errors, the
  request is rejected rather than waved through — so a captured request
  cannot be replayed even within the 5-minute timestamp window.
- **Only one of the three supported LLM providers has a dedicated secret-scan
  rule.** `.gitleaks.toml` sets `useDefault = true`, so the upstream gitleaks
  ruleset applies and the three custom rules (the A2A partner API key, Anthropic
  keys, GitLab PATs) sit on top of it. Broad formats are therefore covered
  generically — the repo's own allowlist records a `generic-api-key` hit — but a
  key for `partner_ainxt_api_key`, whose format is defined by whatever gateway
  an operator points at, matches no targeted rule and is caught only if it trips
  a default heuristic. Treat per-provider coverage as uneven, not absent.
- Dependencies include several LGPL-licensed packages — see
  [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md) if that matters to you.

### What is done well, for balance

- **Signature comparison is constant-time** (`hmac.compare_digest`).
- **The nonce path fails closed.** If the nonce store errors, `verify()` logs
  critical and rejects rather than waving the request through. The previous
  `HMAC_FAIL_OPEN` escape hatch was removed deliberately.
- **The Python image installs from a hash-locked closure** with
  `--require-hashes`, so a substituted artifact at a pinned version fails the
  build.

---

## Cryptography

The A2A envelope is HMAC-SHA256 over a canonical string-to-sign, verified with a
constant-time compare inside a 5-minute timestamp window. Replay protection is
**wired and active**: a database-backed nonce store
(`a2a_common/nonce_store_db.py`, Redis-backed where deployed) rejects reuse and
fails closed. Session tokens are HS256. Passwords are bcrypt.

That symmetric layer needs no post-quantum migration: HMAC-SHA256 and bcrypt are
not threatened by Shor's algorithm, and the Grover speed-up is answered by the
key sizes already in use. TLS is terminated by whatever proxy you deploy in
front, so the transport posture — including any hybrid post-quantum key
exchange — is yours to set, not this repository's.

The A2A contract code is mirrored with the Authority platform. **A change to
`hmac_signer.py` must land on both sides as a coordinated release**, or live
calls start failing signature verification while both test suites still pass.

---

## Hardening the Repository Itself

`gitleaks` runs on every push and pull request
([`.github/workflows/secret-scan.yml`](.github/workflows/secret-scan.yml)),
scanning both the working tree and the full history, and blocks the build on a
hit. Its rule coverage is narrow (see "Known gaps" above), so these still apply:

- Never commit `backend/.env`. Only `backend/.env.example`, with empty values,
  belongs in git.
- Authority-issued credentials go through the Settings UI, never into a file
  under version control.
- The example partner profiles in `data/` are illustrative. Do not replace them
  with a real internal capability document and commit it.

Widening the `gitleaks` rule set to cover the remaining provider key formats is
a welcome contribution — see [`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## License

This security policy is part of the Partner Platform, licensed under the MIT License.
See [LICENSE](LICENSE) for details.
