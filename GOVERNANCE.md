# Partner Platform — Governance

This document describes how the Partner Platform project is governed — how
decisions are made, who the maintainers are, and how the community can
participate.

---

## Model

**Single-vendor open source.** The National Payments Corporation of India (NPCI)
owns the copyright, employs the maintainers, and makes the final call on scope,
architecture and releases.

This is stated plainly because the alternative — implying a neutral,
multi-stakeholder foundation that does not exist — wastes contributors' time.
If that changes, this document changes with it.

### Relationship to AtOM

This repository is the **partner-side** counterpart to
[AtOM](https://github.com/npci/atom-network-platform), the Authority
platform. They are separate repositories with the **same copyright holder, the
same maintainer team, and the same governance**.

They are separate because they have different audiences: the Authority runs one
instance of its platform, while every partner forks this one. But the A2A
contract in `backend/app/a2a_common/` is mirrored between them, so contract
decisions are made once and applied to both. Where this document and
AtOM's `GOVERNANCE.md` differ, the difference is deliberate and
noted; otherwise assume they are aligned.

---

## Principles

- **Open development** — all technical discussion happens in public issues and
  merge requests.
- **Consensus-seeking** — we prefer rough consensus over voting; objections are
  taken seriously.
- **Meritocracy** — influence is earned through sustained, quality contributions.
- **Transparency** — decisions and their rationale are documented publicly.
- **Forkability first** — this is reference base code. A decision that makes the
  upstream neater but a partner's fork harder to maintain is the wrong decision.

---

## Roles

### Users

Anyone who runs the Partner Platform — in practice, ecosystem partners (banks,
PSPs, TPAPs) who have forked it. Users are the most important people in the
project; their integration experience drives priorities.

### Contributors

Anyone who has submitted a merge request that was merged, filed a bug report
that led to a fix, or improved documentation.

### Maintainers

Maintainers have write access to the repository and are responsible for:

- Reviewing and merging merge requests
- Triaging issues
- Cutting releases
- Keeping the A2A contract synchronised with the Authority platform
- Enforcing the [Code of Conduct](CODE_OF_CONDUCT.md)

Current maintainers:

| Name | Handle | Areas |
|---|---|---|
| unifiedagentnxt-admin | [@unifiedagentnxt-admin](https://github.com/unifiedagentnxt-admin) | Overall; A2A contract, agent framework |

`unifiedagentnxt-admin` is a shared maintainer identity operated by the team
behind both platforms, not an individual. Mail reaches the same inbox as the
contacts in [`SECURITY.md`](SECURITY.md).

Areas are a routing hint for reviewers, not ownership — any maintainer may
review anything. A single-maintainer project is honest but should say so rather
than imply a team.

---

## Decision Making

### Day-to-day decisions

Maintainers make day-to-day decisions (bug fixes, minor features, dependency
updates) by consensus in merge request reviews. An MR can be merged when at
least one maintainer approves and no maintainer objects within 48 hours.

### Significant changes

Significant changes (new features, breaking changes, architecture decisions, new
dependencies) require:

- An issue or discussion opened for community input
- At least 2 maintainer approvals
- A 5-business-day comment period before merging

### Breaking changes

Breaking changes additionally require:

- A deprecation notice in the prior release (where feasible)
- An entry in the release notes under a `Breaking Changes` heading
- A major or minor version bump per [Semantic Versioning](https://semver.org/)

### Specific categories

- **Bug fixes, tests, docs** — any maintainer may merge after one review.
- **New features, dependencies, schema changes** — maintainer consensus; a
  single objection blocks until resolved in the issue.
- **Changes to `backend/app/a2a_common/`** — this is the wire contract and it is
  mirrored with the Authority platform. Such a change is **never merged here
  alone**. It needs an issue with rationale, an agreed coordinated release, and
  the matching change landed on the Authority side. Both test suites pass
  regardless of whether you did this, which is exactly why it is a governance
  rule and not a CI check.
- **Changes to the agent contract** (`app/agents/base.py`, the loader, the
  registry, the remote-agent HTTP shape) — this is the public API that every
  fork builds against. Breaking it silently breaks every partner. Treat as a
  breaking change.
- **Licence, trademark, governance** — the copyright holder decides.

Disagreement is resolved in the issue thread, in public. If it cannot be, the
maintainers decide by majority; a tie goes to the status quo.

---

## Becoming a Maintainer

Contributors who have made sustained, high-quality contributions over at least
3 months may be nominated as maintainers by an existing maintainer. Nomination
requires approval from a majority of current maintainers.

Because this repository is forked by organisations that are also its users, a
maintainer drawn from a partner organisation is a good outcome, not a conflict —
provided reviews stay in the open and no partner-specific logic lands upstream.

Maintainers who are inactive for 6 months may be moved to emeritus status.

---

## What Belongs in This Repository

A recurring governance question for reference base code, answered once here and
in more detail in [`CONTRIBUTING.md`](CONTRIBUTING.md):

The platform tier, the contract tier and the agent **framework** are upstream's
responsibility. Individual agent **bodies**, prompts and capability profiles are
the fork's. A contribution that encodes one organisation's process into the
shipped agents will be declined, however good the code — not as a judgement on
the code, but because every other partner then carries it.

---

## Releases

Semantic versioning, with the **A2A contract and the agent contract** as the
public API.

- `0.x` — both contracts may break between minors. They are not stable and do
  not pretend to be.
- `1.0` — not before the A2A contract has been stable across at least one full
  change-management cycle with real partners, and the agent contract has been
  implemented by someone outside the founding team.

Contract-affecting releases are coordinated with the Authority platform and
should be announced with enough notice for partners to re-sync their forks.

Release cadence is monthly **at most**, and less if there is nothing worth
shipping.

---

## Security Issues

Security vulnerabilities are handled privately. See [SECURITY.md](SECURITY.md)
for the responsible disclosure process. Security reports are prioritised over
everything else.

---

## Code of Conduct

All participants are expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
Maintainers are responsible for enforcement.

---

## Support

Best-effort. No SLA. See [SUPPORT.md](SUPPORT.md) for where to ask what.

Partners running a fork in production should not assume upstream will fix their
fork. Upstream fixes upstream; re-basing is yours.

---

## Amendments

This governance document may be amended by a merge request with approval from a
majority of current maintainers and a 5-business-day comment period.

---

*Last updated: 2026-08-24*
