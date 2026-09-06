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

- **Never expose the backend directly.** Put it behind a reverse proxy that
  terminates TLS. Binding port 8011 on a public interface exposes the API with
  no transport protection.
- **Change the seeded admin password immediately.** The default is
  `admin` / `Admin@1234`, it is documented in the README and the deployment
  guide, and it is therefore public knowledge. Rotate it before the service is
  reachable by anyone else.
- **Set `SESSION_JWT_SECRET` explicitly.** It has no safe default. Sessions
  signed with a guessable secret are forgeable.
- **Protect the Authority-issued credentials.** The partner API key and the
  JWT/HMAC secrets are entered through the Settings UI and stored per
  deployment. Anyone with admin access to this platform can act as your
  organisation on the A2A wire.
- **Give the platform the narrowest GitLab token that works.** A token that can
  push to a default branch is a token an injected prompt can try to use. It
  needs enough to create a branch and open a merge request, and no more.
- **Set `CORS_ORIGINS` to your actual frontend origin.** Wildcard CORS was
  removed deliberately; do not put it back.

### Known gaps

We would rather tell you than have you find out:

- **The dependency lock has drifted from `requirements.txt`, and the lock is
  what ships.** `requirements.txt` pins `fastapi` 0.141.1 / `starlette` 1.3.1
  and has replaced `python-jose` with `PyJWT`; `requirements.lock` still
  resolves `fastapi` 0.115.5, `starlette` 0.41.3, `python-jose` 3.4.0 and
  `ecdsa` 0.19.2. Because the image installs from the lock with
  `--require-hashes`, **the intended fixes are not in the built image** —
  including the reachable Starlette advisories and the unfixed `ecdsa`
  CVE-2024-23342.

  This drift is not merely stale, it is **inconsistent with the source**:
  `app/api/auth.py` and `app/a2a_common/auth_middleware.py` both `import jwt`,
  which is PyJWT, and PyJWT appears nowhere in the lock. `python-jose` provides
  `jose`, not `jwt`, and no other locked package provides a top-level `jwt`
  module. An image built strictly from the current lock therefore fails on
  import rather than starting with weaker crypto — which is the safer of the two
  failure modes, but it does mean **the lock has not been exercised since the
  PyJWT migration**. Regenerate it (command in its header) and rebuild before
  any release. Tracked in
  [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md).
- **Login lockout and the JWT denylist are in-memory and single-instance.**
  `backend/app/api/auth.py` keeps both in process dictionaries. Run more than
  one replica and a revoked token stays valid on the other replicas, and
  lockout counters are per-process. There is no CAPTCHA on login — the tiered
  lockout (5 failures → 60s, 10 → 5 min) is the only brute-force control.
- **Nonce replay protection is not active on this side.**
  `hmac_signer.verify()` supports a Redis-backed nonce check, but
  `hmac_middleware.py` calls it with `redis_client=None` — the partner stack
  ships no Redis, and none is in `docker-compose.yml`. Replay is therefore
  bounded only by the **5-minute timestamp window**: a captured request can be
  replayed within that window and will verify. The signature itself still has to
  be valid, so this is a replay exposure, not a forgery one. If that window is
  too wide for your risk appetite, add a Redis service and pass the client
  through; the code path already exists and fails closed.
- **No CI, no secret scanning, and no hygiene gate in this repository.** The
  Authority platform gates every change with `gitleaks` and a hygiene script;
  this one has neither yet. Until that lands, credential hygiene here is manual.
- Dependencies include several LGPL-licensed packages — see
  [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md) if that matters to you.

### What is done well, for balance

- **Signature comparison is constant-time** (`hmac.compare_digest`).
- **The nonce path fails closed where it is wired.** If a Redis client is
  supplied and Redis then fails, `verify()` logs critical and rejects rather
  than waving the request through. The previous `HMAC_FAIL_OPEN` escape hatch
  was removed deliberately.
- **The Python image installs from a hash-locked closure** with
  `--require-hashes`, so a substituted artifact at a pinned version fails the
  build. The problem above is that the lock is stale, not that it is absent.

---

## Cryptography

The A2A envelope is HMAC-SHA256 over a canonical string-to-sign, verified with a
constant-time compare inside a 5-minute timestamp window. A Redis-backed nonce
check for replay protection is implemented but **not wired on this side** — see
*Known gaps*. Session tokens are HS256. Passwords are bcrypt.

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

There is currently no automated secret scanning here. Until one is added:

- Never commit `backend/.env`. Only `backend/.env.example`, with empty values,
  belongs in git.
- Authority-issued credentials go through the Settings UI, never into a file
  under version control.
- The example partner profiles in `data/` are illustrative. Do not replace them
  with a real internal capability document and commit it.

Adding `gitleaks` and a CI pipeline matching the Authority platform's is a
welcome contribution — see [`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## License

This security policy is part of the Partner Platform, licensed under the MIT License.
See [LICENSE](LICENSE) for details.
