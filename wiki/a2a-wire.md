# The A2A wire

> **Verified at:** commit `4d4999c`, 2026-09-03. Task types counted in
> `backend/app/a2a_common/protocol.py`; handlers listed from
> `backend/app/a2a_common/handlers/`.
>
> For **authentication** on this wire, see [security layers](security-layers.md).
> This page is the protocol and the code-sharing mechanism.

## This code is not yours

Three modules under `backend/app/a2a_common/` are **generated into** this tree
from the Authority repository, not written here:

| Module | What it does |
|---|---|
| `hmac_signer.py` | Computes and verifies the envelope signature |
| `protocol.py` | The task-type vocabulary and envelope reader |
| `executor_base.py` | The dispatch skeleton |

Each carries a header saying so:

```
# GENERATED FILE — DO NOT EDIT HERE. Your change will be overwritten.
#
# Canonical source: packages/a2a-core/a2a_common/protocol.py
```

The canonical copies live in the Authority repository's `packages/a2a-core/`,
and its `MANIFEST` lists where each one lands. **That manifest does not cover
this repository.** The Authority's hygiene gate cannot see these files, so
nothing automatically detects drift between them and canonical — keeping them in
step is a release-coordination duty, not a CI check.

That asymmetry is worth internalising. On the Authority side, editing a vendored
copy is caught. Here it is not.

**Why the machinery exists:** every service hashes the same wire bytes. Change
what one side feeds the signer — field order, encoding, which headers are
covered — and signatures stop matching *across a trust boundary*. The symptom
appears on the other side of the boundary from the edit, as a generic
authentication failure. Making the file physically un-editable in place is
cheaper than diagnosing that twice.

## The message vocabulary

**37 task types**, each declared with a direction and an expected cardinality —
"once per change and version", "any, per question" — in a single table in
`protocol.py` rather than implied across handler code.

Having cardinality declared next to the type is what makes duplicate-delivery
handling reviewable. A handler that quietly accepts a second
`change_acknowledgement` for the same version is a bug you can see in the table,
not one you have to infer from code.

The first 28 are the frozen contract; the remainder extend it — five for the
certification lifecycle, and three for the integration-testing tunnel
(`http_exchange_request`, `http_exchange_response`, `cert_execution_start`). They group into distribution, acknowledgement, queries
and clarifications, progress and readiness, negotiation, and certification.

> **Counting caution.** The Authority repository contains a *second* enum also
> named `A2ATaskType`, in `backend/app/models/phase_c.py`, with a different and
> smaller membership. It is a persistence-layer record, not the wire vocabulary.
> If you are reconciling counts across the two repositories, make sure you are
> reading `a2a_common/protocol.py` on both sides — this repository's copy is
> byte-identical to canonical apart from the generated header.

## What arrives, and what handles it

Inbound messages dispatch to `a2a_common/handlers/`:

| Handler | Triggered by |
|---|---|
| `change_communication` | A new change, or a new version of one |
| `clarification_response` | The Authority answering a query you raised |
| `counter_decision` | The Authority accepting or rejecting your counter |
| `round_opened` / `round_closed` | Negotiation round boundaries |
| `negotiation_frozen` | Negotiation terminated |
| `revision_in_progress` | The Authority is revising the change |
| `blocker_resolution` / `blocker_status_update` | Movement on a blocker you raised |
| `cert_lifecycle` | Certification setup, config and run control |
| `cert_test_response` | A test case result |
| `cert_completion_signoff` | Certification closed |

`_background.py` is the shared mechanism for work a handler must not do inline;
`_types.py` holds the shared shapes.

## Two ends, not two peers

The Authority and this platform are **not symmetric**. `client.py` and
`mount.py` differ per service by design: one is the authority, the other a
participant. They are not vendored, and no gate compares them.

Concretely, the middleware here is lighter than the Authority's counterpart in
three ways, each documented in the module that makes the choice:

- **No session-revocation lookup.** Partners verify JWTs the Authority minted;
  revocation is centralised at the issuer.
- **No partner-registry lookup.** This stack receives from exactly one upstream,
  so the secret is global per-stack rather than per-caller.
- **No Redis nonce store for replay.** Most partner deployments do not run
  Redis, so replay defence rests on the timestamp window alone.

That last one is a real, deliberate reduction in defence, not an oversight —
see [security layers](security-layers.md).

## The endpoint

JSON-RPC is mounted at `/a2a-rpc/rpc`. The agent card is advertised
**unprefixed at the root**, because a remote agent fetches the card without
knowing anything about local path layout; publishing it under a prefix once
broke discovery outright.

Task-store state is persisted via `task_store_db.py`, so a task is not lost when
a process restarts mid-conversation. Note that `main.py` still passes
`task_store=None` at mount time with a comment saying the swap is pending —
check which is live in your deployment before relying on restart survival.

## The one that bites

**Outbound `Message.task_id` means "continue this existing task."** Setting it
on a first send asks the remote side to continue a task it has never heard of,
and it answers exactly that: *task does not exist*. Leave it empty for new
sends.

This is the single most-repeated mistake against this wire, and it reads like a
server bug when you hit it.

## The second one that bites

**Clock drift presents as an authentication failure.** Inbound messages carry a
timestamp checked against a five-minute window. Because this stack has no nonce
store, that window is doing more work here than on the Authority side. If
traffic starts failing for no apparent reason and nothing was deployed, check
NTP on both hosts before reading any code.

## Related

- Authentication and rejection codes: [security layers](security-layers.md)
- What flows across it, in business terms: [change lifecycle](change-lifecycle.md)
- Where the boundary sits: [architecture](architecture.md)
