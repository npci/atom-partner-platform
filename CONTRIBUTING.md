# Contributing to the Partner Platform

Thank you for your interest in contributing! This document explains how to get
involved, what we expect from contributors, and how the review process works.

> **This repository is reference base code.** It is meant to be forked and have
> its agent bodies replaced with your own logic. Most of what you build on top
> of it is yours and does not belong upstream — see
> [What belongs upstream](#what-belongs-upstream) before opening a pull request.

> **This project is pre-1.0 and the A2A contract tracks the Authority platform —
> expect breaking changes.** Contributions are accepted under a **DCO** (see
> [Developer Certificate of Origin](#developer-certificate-of-origin-dco)) —
> no paperwork, just `git commit -s`.

---

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [What belongs upstream](#what-belongs-upstream)
- [Ways to Contribute](#ways-to-contribute)
- [Getting Started](#getting-started)
- [Development Workflow](#development-workflow)
- [Commit Guidelines](#commit-guidelines)
- [Developer Certificate of Origin (DCO)](#developer-certificate-of-origin-dco)
- [Pull Request Process](#pull-request-process)
- [Coding Standards](#coding-standards)
- [Testing](#testing)
- [Architecture Rules Worth Knowing Early](#architecture-rules-worth-knowing-early)
- [Contributing an Agent](#contributing-an-agent)
- [License](#license)

---

## Code of Conduct

By participating in this project you agree to abide by our
[Code of Conduct](CODE_OF_CONDUCT.md). Please read it before contributing.

---

## What belongs upstream

This repository is deliberately a starting point, so the line matters more here
than in a typical project.

**Belongs upstream:**

- Fixes to the platform tier — transport, persistence, auth, the UI.
- Fixes or improvements to the agent *framework*: the base class, loader,
  registry, audit trail, remote-agent HTTP contract.
- Contract fixes in `app/a2a_common/`, coordinated with the Authority platform
  (see [Architecture rules](#architecture-rules-worth-knowing-early)).
- Documentation, tests, dependency and security updates.

**Does not belong upstream:**

- Your organisation's capability profile, prompts, or agent bodies. Those are
  the parts you are expected to replace in your fork.
- Integrations with infrastructure specific to one partner.

If you are unsure, open an issue before writing the code. A rejected pull
request is a worse outcome for you than a five-minute question.

---

## Ways to Contribute

- **Bug reports** — open an issue describing what you expected and what happened.
- **Feature requests** — open an issue explaining the use case, not just the
  proposed solution.
- **Documentation** — fix typos, improve clarity, add examples.
- **Code** — fix bugs, implement features, improve performance.
- **Reviews** — review open merge requests and provide constructive feedback.

---

## Getting Started

### Prerequisites

- Python 3.12+
- Node.js 20+
- Docker & Docker Compose (for full-stack local dev)
- PostgreSQL 16+ with pgvector (supplied by Compose)

### Local Setup

```bash
git clone https://github.com/npci/atom-partner-platform
cd partner-platform
cp backend/.env.example backend/.env    # set SESSION_JWT_SECRET; LLM key optional
docker compose up -d
```

Open <https://localhost:8443/a2a-partner/>. The default admin is `admin` / `Admin@1234` —
**change it immediately**, and never carry it into a deployed environment.

With no LLM key configured the feasibility agent returns mock output, so the
stack works end to end on a fresh clone without any credentials.

Read [`ARCHITECTURE.md`](ARCHITECTURE.md) before your first change. It is the
repository's working manual: the three tiers, the agent contract, bindings, the
config/secret split, and a "build your own agent" walkthrough.

---

## Development Workflow

1. **Create a branch** from `main`:

   ```bash
   git checkout -b feat/my-feature   # or fix/my-bug
   ```

2. **Make your changes** — keep commits focused and atomic.

3. **Run tests and linting** before pushing (see [Testing](#testing)).

4. **Push and open a merge request** against `main`.

5. **Address review feedback** — maintainers may request changes.

6. **Merge** — a maintainer will merge once approved.

---

## Commit Guidelines

We follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short summary>

[optional body]

[optional footer — DCO sign-off goes here]
```

**Types:** `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`, `ci`

**Examples:**

```
feat(agents): add retry with backoff to the remote-agent binding
fix(a2a): correct HMAC canonicalisation for empty-body requests
docs(architecture): document the agent audit table
```

Keep the summary line under 72 characters. Use the body to explain *why*, not
*what* (the diff shows what).

---

## Developer Certificate of Origin (DCO)

This project uses the **Developer Certificate of Origin (DCO)** instead of a
Contributor License Agreement (CLA). By signing off your commits you certify
that you have the right to submit the contribution under the MIT license.

**Sign off every commit** with `-s`:

```bash
git commit -s -m "fix(agents): guard against a missing prompt file"
```

This appends:

```
Signed-off-by: Your Name <your.email@example.com>
```

The full DCO text is in the [DCO.md](DCO.md) file at the root of this
repository.

> **Note:** merge requests without a DCO sign-off on every commit will not be
> merged. If you forgot, you can amend: `git commit --amend -s` (for the last
> commit) or `git rebase --signoff HEAD~N` (for the last N commits).

A DCO was chosen over a CLA deliberately: a CLA would let the copyright holder
relicense contributed code later, but it needs legal administration and is a
documented deterrent to casual contributors. If a relicensing need ever arises,
it will be negotiated with contributors rather than pre-empted here.

---

## Pull Request Process

1. Describe the change and the reasoning; link the related issue.
2. Ensure tests and lint pass locally.
3. Request a review from the relevant code owners (see [`CODEOWNERS`](CODEOWNERS)).
4. Do not merge your own MR — at least one maintainer approval is required.

### Before you open a merge request

```bash
cd backend
pip install -r requirements.txt -r requirements-dev.txt
pytest                                   # agent-contract, handler, and e2e tests
ruff check .
cd ../frontend && npm ci && npm run build
```

If you changed a dependency pin, regenerate the hash lock as well — the command
is in the header of `backend/requirements.lock`. **Editing
`backend/requirements.txt` alone has no effect on the built image**, which
installs from the lock. This is a live source of drift in this repository; see
[`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md) for the current gap.

### MR Size Guidelines

| Size   | Lines changed | Guidance                                      |
|--------|---------------|-----------------------------------------------|
| Small  | < 200         | Preferred — fast to review                    |
| Medium | 200–500       | Fine — include a clear description            |
| Large  | > 500         | Split if possible; add a detailed description |

---

## Coding Standards

### Python

- **Linter and formatter:** `ruff` (config in `backend/pyproject.toml`,
  line length 100)
- **Type hints:** required for all public functions and class methods
- **Docstrings:** Google style for public APIs

```bash
cd backend
ruff format .
ruff check . --fix
```

### JavaScript / React

The frontend is plain React with Vite. Match the surrounding style, keep
components small, and check [`frontend/DESIGN_SYSTEM.md`](frontend/DESIGN_SYSTEM.md)
before introducing new visual patterns.

```bash
cd frontend && npm run build
```

### General

- No hardcoded secrets, credentials, or internal hostnames — use env vars.
- No `verify=False` on TLS connections.
- No wildcard CORS (`allow_origins=["*"]`) in production code.
- Keep `.env` out of commits.
- Authority-issued credentials are configured through the Settings UI and
  stored per deployment. Never commit one, not even an expired one.

### What we look for in reviews

- **Verify behaviour, not just types.** "The tests should pass" is not "I ran
  the tests". For UI changes, run the stack and click through.
- **Surgical diffs.** A bug fix should not reformat the file. If you spot
  unrelated dead code, mention it; do not remove it in the same commit.
- **Explain *why* in the commit body.** The subject says what changed; the body
  says why now, and what you considered instead.
- **No new abstractions until the third instance.** Two similar blocks is not
  duplication worth a framework.
- **Respect the tier boundary.** The agent tier receives plain dicts and returns
  plain dicts. Do not let a DB or ORM object cross into `app/agents/` — that
  seam is what makes agents swappable, and it is the reason this repository
  exists.

---

## Testing

```bash
cd backend
pytest                                   # full suite
pytest tests/test_agent_framework.py     # a single file
```

Or inside Compose:

```bash
docker compose run --rm backend pytest
```

The suite covers the agent contract, handler smoke tests, dashboard routes, the
partner-profile loader, the remote-agent binding, and an end-to-end flow.

New features must include tests. Bug fixes should include a regression test.

---

## Architecture Rules Worth Knowing Early

- **`app/a2a_common/` is Tier 1 — the contract. Do not edit it casually.** It is
  **mirrored** between this repository and the Authority platform
  ([AtOM](https://github.com/npci/atom-network-platform)), and
  `hmac_signer.py` must stay byte-identical: both sides hash the same wire
  bytes.

  Each repository's CI validates only its own copy. **Nothing checks the two
  against each other**, so a signing change must be landed on both sides as a
  coordinated release. Skip that and both test suites still pass — the first
  symptom is a rejected signature on a live A2A call.

- **Dependency direction is one-way:** Platform → Agent (via the registry) and
  Platform → Contract. The orchestrator only ever calls
  `registry.get("<name>").execute(...)`; it never imports a concrete agent.

- **Every agent run is audited.** `Agent.execute()` owns the lifecycle and the
  `agent_runs` row. Override `run()`, not `execute()`.

- **Prompts live in `app/agents/prompts/*.md`, not in Python.** A prompt change
  is a reviewable diff, and keeping it that way is deliberate.

- **Never interpolate a settings key into a log line — wrap it in
  `safe_key_label()`.** Any key in `core.secret_box.SECRET_KEYS`
  (`npci_hmac_secret`, `partner_api_key`, …) must be passed through
  `core.secret_box.safe_key_label()` before it reaches a logger:

  ```python
  from app.core.secret_box import safe_key_label

  logger.critical("failed to decrypt %s", safe_key_label(key))   # ✅
  logger.critical("failed to decrypt %s", key)                   # ❌ fails CI
  ```

  The key is only a *name*, not the secret — but to a taint-tracking scanner a
  variable holding the name is indistinguishable from one holding the value, so
  the second form trips Checkmarx "Filtering Sensitive Logs". It has been
  reported against this repo twice; the second time was a fix that looked
  correct but wasn't. `backend/tests/test_no_raw_secret_key_logging.py` now
  enforces this, so you get a CI failure in minutes instead of a scan finding in
  weeks.

  Two things to know if you touch this area:

  - **`safe_key_label()` is written as a chain of `if key == "...":` returns on
    purpose.** Do not "tidy" it into a dict lookup. Checkmarx treats indexing a
    container with a tainted key as taint propagation, so the table form does
    not clear the scan — that is precisely how the first fix failed. A test
    guards the shape, not just the behaviour.
  - **Adding a new secret means two edits:** add it to `SECRET_KEYS`, and give
    it a label in `safe_key_label()`. A sync test fails if you do only the
    first.

  If you just need to confirm a value was present, log `len(...)` — a length
  carries no secret and is already the established pattern in `settings.py`.

---

## Contributing an Agent

The five shipped agents are reference implementations. In your own fork you will
replace their bodies — that is the intended use, and it needs no upstream
change.

An agent contributed *upstream* has a higher bar: it must be generic across
partners, carry tests, and not encode any one organisation's process. Implement
`app.agents.base.Agent`, register it in `backend/config/agents.yaml`, and bind
it either in-process (`impl:`) or over HTTP (`url:`).

See the "build your own agent" walkthrough in
[`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## License

By contributing to the Partner Platform, you agree that your contributions will
be licensed under the [MIT License](LICENSE).

You retain copyright of your contributions. The DCO sign-off certifies that you
have the right to submit the work under this license.
