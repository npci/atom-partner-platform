# Getting Support

This document explains where to get help with the Partner Platform.

---

## Start with the documentation

Most questions are answered in one of four places:

| Document | Covers |
|---|---|
| [`README.md`](README.md) | Quick start, first-time setup, the A2A surface, key environment variables |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | The three tiers, the agent contract, bindings, config/secret split, LLM key resolution, the audit table, prompt customisation, and a "build your own agent" walkthrough |
| [`DEPLOYMENT_GUIDE.md`](DEPLOYMENT_GUIDE.md) | Deploying the stack, including bare-metal and Docker |
| [`frontend/DESIGN_SYSTEM.md`](frontend/DESIGN_SYSTEM.md) | UI conventions if you are changing the frontend |

If you are about to plug in your own agent, read `ARCHITECTURE.md` first. It is
the document this repository exists to make actionable.

---

## Which channel for which question

| Your question | Where it goes |
|---|---|
| "I found a security vulnerability" | **Not** a public issue — see [`SECURITY.md`](SECURITY.md) |
| "The platform has a bug" | Open an issue with reproduction steps |
| "I want a feature" | Open an issue describing the use case |
| "How do I write my own agent?" | `ARCHITECTURE.md`, then an issue if it is still unclear — an unclear walkthrough is a documentation bug |
| "My fork's agent doesn't work" | Your fork, unless you can reproduce it against unmodified upstream |
| "My Authority credentials are rejected" | Your onboarding contact at the Authority, not this repository |
| "Can this integrate with X?" | Open an issue; it may already be possible through the remote-agent binding |

Before opening an issue, please search existing issues to avoid duplicates.

---

## A useful bug report

- What you expected, and what happened instead.
- Whether you are running unmodified upstream or a fork — and if a fork, whether
  you reproduced it on upstream.
- The commands you ran (`docker compose up -d`, or the manual backend path).
- Relevant logs, with credentials and partner identifiers redacted.

**Never paste an Authority-issued API key, JWT, or HMAC secret into an issue.**
If you think one has been exposed, treat it as a security incident and follow
[`SECURITY.md`](SECURITY.md).

---

## Contributing

Contributions are welcome. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the
workflow, the DCO sign-off requirement, and — importantly for reference base
code — what belongs upstream versus in your fork.

---

## What is not supported here

- **Your fork.** This is reference base code, intended to be modified. Once you
  replace the agent bodies, that logic is yours to support.
- Deployment on specific proprietary infrastructure.
- Authority-side issues, onboarding, or credential provisioning — those go
  through your Authority contact, or against
  [AtOM](https://github.com/npci/atom-network-platform) for platform
  bugs on that side.
- Custom integrations beyond the documented extension points.

---

## Expectations

Support is **best-effort with no SLA** — see [`GOVERNANCE.md`](GOVERNANCE.md).
Security reports are prioritised over everything else.

*For enterprise support options, contact the maintainers via the repository.*
