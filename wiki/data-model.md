# Data model

> **Verified at:** commit `0a72eb1`, 2026-09-03. **24 tables** counted in
> `backend/app/models.py`; **31** `_ensure_*` helpers in
> `backend/app/database.py`.
>
> This page is a snapshot, not a generated projection — see below.

## There is no migration tool

The Authority repository runs Alembic and can name a schema head. **This
repository does not.** Schema management is deliberately lightweight, and the
docstring in `database.py` says so outright:

> Schema management remains lightweight (no alembic on partner side)

Two mechanisms do the work at startup:

1. **`Base.metadata.create_all()`** — creates any table that does not exist.
2. **31 `_ensure_*()` helpers** — `ADD COLUMN IF NOT EXISTS` for every column
   added after its table shipped.

The second exists because of a sharp edge in the first, and the code comment
states it exactly:

> `create_all()` above creates NEW tables but never alters EXISTING ones, so
> these columns need explicit ALTERs or an upgrade over an existing volume boots
> into "column does not exist" on the first query.

That is the whole design in one sentence. `create_all()` looks like it manages
schema and does not; every column added to a table that already exists in a
deployed volume needs a hand-written helper, or the upgrade breaks on first
query rather than at startup.

**What this costs you when you fork:** adding a column to an existing table is
two edits, not one — the model *and* a new `_ensure_*` helper wired into the
startup sequence. Forget the second and it works on your laptop (fresh volume,
`create_all` builds the table complete) and fails in every environment that has
data. This is the single most likely way to break an upgrade in this
repository.

The helpers are idempotent by construction — Postgres-native
`ADD COLUMN IF NOT EXISTS`, PG 9.6+ — so running them every boot is free.

## Ordering matters at startup

The sequence is not arbitrary:

```
create_all()
  → column ensures (existing tables)
  → pgvector extension
  → embedding_cache, document_chunks
  → seed partner profile
  → sweep interrupted agent jobs
```

The vector tables come after the extension because they depend on it. The
**interrupted-job sweep** comes last and is easy to miss: `agent_jobs` left
`running` by a process that died are reconciled at boot, so a crash mid-agent
does not leave a job that never resolves.

## The tables

| Group | Tables |
|---|---|
| **The change** | `incoming_changes`, `change_documents`, `change_test_data` |
| **Talking back** | `outgoing_queries`, `query_drafts`, `progress_reports` |
| **Agent work** | `agent_runs`, `agent_jobs` |
| **Agent output** | `feasibility_reports`, `design_reports`, `code_reports`, `test_reports`, `code_review_reports` |
| **Code** | `code_repos`, `code_merge_requests`, `generated_code_files` |
| **Knowledge** | `knowledge_docs` (+ `document_chunks`, `embedding_cache` — created outside the models) |
| **Config & identity** | `partner_settings`, `partner_profiles`, `partner_users` |
| **Delivery** | `outbound_a2a_retries` |
| **Certification & tunnel** | `cert_fix_rounds`, `integration_exchanges`, `cert_case_executions` |

Two of the vector tables — `document_chunks` and `embedding_cache` — are
created by raw SQL helpers rather than declared as models, so they will not
appear if you enumerate `Base` subclasses. If you are auditing the schema, read
`database.py` as well as `models.py`.

## JSON columns carry structure the schema does not

Several of the most important fields are `Text` holding JSON, not relational
structures — particularly on `incoming_changes`:

| Column | Holds |
|---|---|
| `npci_counter` | The *active* counter-proposal, `NULL` when none is open |
| `npci_counter_history` | Append-only log of every counter and how it resolved |
| `counter_decisions` | Append-only log of the Authority's accept/reject responses |
| `blockers` | Append-only, each with severity, impact and resolution |
| `npci_followups`, `round_notices`, `emergency_issues` | Append-only message logs |

**These are append-only by design, for non-repudiation.** The model comments say
so directly. It is tempting to normalise them into tables when you fork — that
is a reasonable change, but preserve the append-only property, because both
sides rely on being able to prove what was proposed in which round.

The trade-off you inherit: none of this is queryable in SQL without JSON
operators, and none of it is constrained by the schema. A malformed entry is a
runtime problem, not a write-time one.

## `partner_settings` is the runtime config surface

One table, key-value, holding the Authority JWT and HMAC secrets, the partner
API key, LLM keys and repository tokens. **Every secret in it is encrypted at
rest** via `core/secret_box.py`, and the platform refuses to write one before an
encryption key is configured.

Key names are centralised in `core/setting_keys.py` rather than spelled as
string literals at each call site — check there before adding one.

## Seeding

`_ensure_seed_partner_profile()` writes a starter `partner_profiles` row on
first boot, so the feasibility agent has something to reason against rather than
failing on an empty profile. It is a placeholder: **a thin profile produces thin
assessments**, and replacing it is part of first-run setup, not optional
polish.

## Related

- What flows into these tables: [change lifecycle](change-lifecycle.md)
- How `document_chunks` is populated and queried: [retrieval](retrieval.md)
- Running and upgrading: [`../DEPLOYMENT_GUIDE.md`](../DEPLOYMENT_GUIDE.md)
