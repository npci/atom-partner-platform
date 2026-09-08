# User Guide — Partner Platform

For the people at a partner organisation who use the platform: the team that
receives a specification change from the Authority, decides how to implement it,
tracks the work, and gets it certified.

This is not a developer or operator document. For installation and configuration
see [`DEPLOYMENT_GUIDE.md`](DEPLOYMENT_GUIDE.md); for the agent contract and how
to build your own, see [`ARCHITECTURE.md`](ARCHITECTURE.md).

- [What this platform does for you](#what-this-platform-does-for-you)
- [First run](#first-run)
- [A change, end to end](#a-change-end-to-end)
- [The change screen](#the-change-screen)
- [Talking back to the Authority](#talking-back-to-the-authority)
- [Certification](#certification)
- [Settings and knowledge](#settings-and-knowledge)
- [Reading what the platform tells you](#reading-what-the-platform-tells-you)

---

## What this platform does for you

The Authority — [AtOM](https://github.com/npci/atom-network-platform) — publishes
a specification change. Without this platform, that arrives as documents in an
inbox and someone starts a spreadsheet.

Here, it arrives over an authenticated protocol into a system that already knows
what a change is: it holds the documents, runs your own agents over them to
assess feasibility and draft a design, tracks implementation status, carries
your questions and blockers back to the Authority, and drives certification when
you are ready.

**The agents are yours.** Seven ship as reference implementations. Six carry
real logic — feasibility, design, code, test and the two review lenses; only
`negotiation` is a stub returning mock output. Every one of them falls back to
documented, clearly-labelled mock output when no LLM key is configured, so the
flow is complete on a fresh clone with no credentials. Replacing them with your
own is the intended use of this repository, and it is a one-line change per
agent.

---

## First run

Your administrator will have completed installation. Two things then need doing
before a change can arrive:

**Connect to the Authority.** In **Settings**, supply the Authority platform URL
and the credentials issued to you during onboarding. Use **Test Connection** to
confirm the link before waiting on a change that never comes.

**Set your capability profile.** This describes what your organisation supports,
and is what the feasibility agent reasons against. A thin profile produces thin
assessments.

If the connection test fails, the message names the layer that refused rather
than saying "failed" — see [Reading what the platform tells you](#reading-what-the-platform-tells-you).

---

## A change, end to end

```
   Authority publishes
          ↓
   Change arrives  →  Acknowledge  →  Assess  →  Implement  →  Certify
          ↓                ↓            ↓            ↓            ↓
      Documents        receipt      feasibility   progress    test cases
                                      + design     updates    + sign-off
```

Your assignment moves through a lifecycle the Authority can see:

```
assigned → communicated → acknowledged → in_progress → ready
  → received → accepted → applied → tested
  → ready_for_certification → certifying → certified
```

You also report coarse progress independently — **design complete**, **coding
complete**, **testing complete**. That is what the Authority's readiness view
uses, and it is separate from the lifecycle above so you can say "still working"
without implying a state you have not reached.

---

## The change screen

Open a change from the dashboard. Everything for that change lives here, in
sections:

| Section | What it is for |
|---|---|
| **Documents** | The BRD, technical specification, schemas and product kit the Authority sent |
| **Design** | Your feasibility assessment and design output |
| **Code** | Code generation, review and the merge request |
| **Testing** | Test data, test cases and results |
| **Activity** | Everything that has happened, in order, including every message exchanged |

**Activity is the audit trail.** Every A2A message in and out is recorded, with
its correlation ID. When you and the Authority disagree about what was sent and
when, this is the answer.

### Documents

Read-only — they are the Authority's artifacts, not yours. If something is
unclear or wrong, do not work around it: raise a **query** (below). A query is
answered on the record and the answer reaches everyone; a private assumption
does not.

### Design

The feasibility agent assesses the change against your capability profile and
produces a report. The design agent drafts an approach. Both are advisory — they
are a starting point for your engineers, not a decision.

Without an LLM key configured, these return documented mock output so the flow
still works end to end. Mock output is labelled as such; if you are not sure
whether you are looking at a real assessment, check Settings.

### Code

Where code generation, review and the merge request live. Two review lenses —
`code_reviewer` and `security_reviewer` — run over generated code, and **any
finding from either blocks the merge request**. That gate is deliberate and is
not something the generating agent can clear on its own.

The scope of that gate is documented plainly in
[`ARCHITECTURE.md`](ARCHITECTURE.md), and it is worth reading before relying on
it: the review and fix loop does not generate or run tests. Test generation
exists as an opt-in step and is off by default, precisely so nobody reads more
into a clean review than it claims.

### Testing

Test data for certification cases lives here. You fill in the values for the
cases the Authority sends.

**A case with no data is reported as not ready, not filled with defaults.** If
you have entered data for some cases and not others, the gaps come back marked
`ready=false` with a reason rather than being certified against numbers nobody
chose. That is deliberate — a green certification against invented test data is
worse than an honest gap.

---

## Talking back to the Authority

Four things travel back, and each has its own shape.

### Queries

A question about the change. Free text, answered by the Authority on the record.
Use these liberally — the answer becomes part of the change's history and is
visible to everyone working on it.

### Blockers

Something preventing progress. A blocker has a **severity** and a **status** of
its own, so you can be "in progress but blocked critically" rather than merely
appearing late. Raise one as soon as it is real; a blocker raised early reads as
communication, and the same blocker raised at the deadline reads as a surprise.

### Counter-proposals

If the rollout terms do not work for you — dates, scope, phasing — counter them
rather than accepting and missing.

**Negotiation is a round-based loop and rounds close on a timer.** If you do not
respond before a round closes, the Authority applies **silent acceptance**: your
silence is recorded as agreement. This is the mechanism most worth understanding
in the whole platform, because it is the one where doing nothing has a
consequence.

### Progress updates

Design, coding and testing milestones. Cheap to send and worth sending, because
the alternative is the Authority asking.

---

## Certification

When your implementation is ready, certification exercises it against the
Authority's test cases.

**Two classes of case.** Some are driven by the Authority against your system;
others are initiated by your system against theirs. The set you are responsible
for initiating is identified when the run is set up.

**Results arrive per case.** A failure comes back with the case, the field and
the rule that failed — not just "the case failed". That difference is what makes
a failure fixable in one pass rather than a round of guessing.

**A failure that breaks a documented schema constraint is a real defect**, not
something waivable. A response-code mismatch may be a deployment nuance you can
legitimately request a waiver for; a field that violates its own registry
constraint is not.

**Rounds repeat until zero failures.** Between rounds you fix and resubmit. The
loop stops on success, on a round cap, or when two consecutive rounds fail
identically — two identical rounds mean the fix changed nothing, and a third
will not either.

A human at your organisation approves the round close before the Authority is
told you are ready. That gate is yours and is not automated away.

---

## Settings and knowledge

| Screen | What it manages |
|---|---|
| **Settings** | Authority connection, credentials, capability profile, LLM provider |
| **Users** | Accounts for your organisation |
| **Knowledge** | The document corpus your agents are grounded in |

**Secrets are encrypted at rest.** The Authority JWT and HMAC secrets, your
partner API key, the LLM key and any repository token are encrypted in the
database. Your administrator must set the encryption key before any secret can
be saved — the platform refuses to store one otherwise rather than writing it in
the clear.

**Knowledge decides answer quality.** If agent output looks ungrounded, check
the corpus is populated before suspecting the model. Retrieval quality is not
visible in any output check.

---

## Reading what the platform tells you

**Rejections name the layer.** The A2A boundary has several independent checks —
transport security, credentials, message signature, replay window, rate limit —
and each fails differently and on purpose. A missing secret gives one answer, an
invalid one another, a clock out of step a third. The message tells you which,
so start there rather than with the whole stack.

**Clock drift is a real cause.** Inbound messages carry a timestamp checked
against a five-minute window. If certification traffic starts failing for no
apparent reason, check NTP on both sides before anything else.

**Not ready is not the same as failed.** A test case held back for missing data
is reported as skipped with a reason. It is not a failure and does not count
against you; it is a gap for you to fill.

**Mock output is labelled.** Where an agent is a reference stub rather than your
own implementation, its output says so. Anything unlabelled is real.

---

## Related documents

| Document | Covers |
|---|---|
| [`README.md`](README.md) | Overview, architecture, features, quick start |
| [`FAQ.md`](FAQ.md) | Common questions, especially about forking |
| [`DEPLOYMENT_GUIDE.md`](DEPLOYMENT_GUIDE.md) | Installation |
| [`CONFIGURATION.md`](CONFIGURATION.md) | Settings, and which surface wins |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | The three tiers, the agent contract, build your own |
| [`SECURITY.md`](SECURITY.md) | Reporting a vulnerability |
| [`wiki/`](wiki/) | How the platform works inside |
