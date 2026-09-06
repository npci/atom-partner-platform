# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""`_delete_chunks` must apply exactly the scope it was asked for.

WHY THIS FILE EXISTS
--------------------
The sonatype-2021-0025 hardening replaced a dynamically-built DELETE with a
table of static statements. The first attempt at that table defined only SIX
statements for EIGHT possible combinations of the three optional scope
arguments, and selected between them with an if/elif chain that tested
`repo_id` before `change_id`. When a caller passed both, the chain took the
repo branch and silently dropped `change_id = :cid`, issuing a DELETE strictly
BROADER than requested — six extra rows in a mixed-scope fixture.

That is the failure mode a security-motivated rewrite must never have: the
finding gets marked closed while the code quietly deletes more than it should.
The dynamic version it replaced, for all its taint-analysis problems, always
produced the correct predicate set.

So these tests pin the SCOPING CONTRACT independently of how it is implemented:
the predicates applied are exactly `doc_category` plus one per non-None scope
argument. They are written against observed behaviour (the SQL actually handed
to the session, and the rows actually deleted), not against the dispatch table,
so a future refactor back to Core `delete()` or to any other shape still has to
satisfy them.
"""
from __future__ import annotations

import itertools

import pytest
from sqlalchemy import text

from app.rag.doc_ingest import _DELETE_STATEMENTS, _delete_chunks

# The three optional scope arguments, and the predicate each one must add.
_SCOPES = {
    "change_id": "change_id = :cid",
    "repo_id": "repo_id = :rid",
    "source_key": "source_key = :sk",
}


class _SpySession:
    """Captures the statement and params instead of executing them."""

    def __init__(self) -> None:
        self.statement: str | None = None
        self.params: dict | None = None

    def execute(self, statement, params=None):
        self.statement = str(statement)
        self.params = params
        return None


def _predicates(sql: str) -> set[str]:
    """The set of ANDed predicates in the WHERE clause of `sql`."""
    where = sql.split("WHERE", 1)[1]
    return {p.strip() for p in where.split("AND")}


# ── 1. The dispatch table must be COMPLETE ──────────────────────────────────

def test_dispatch_table_covers_all_eight_combinations():
    """2^3 = 8 keys. This is the assertion the original six-entry table failed.

    A missing key is not a benign gap: with dict dispatch it raises KeyError,
    and with the if/elif chain it used to mean a silently broader DELETE.
    """
    expected = set(itertools.product((False, True), repeat=3))
    assert set(_DELETE_STATEMENTS) == expected, (
        "the DELETE dispatch table is incomplete — every combination of "
        "(change_id, repo_id, source_key) needs its own static statement"
    )


def test_every_dispatch_statement_is_distinct():
    """No two combinations may share a statement.

    Sharing is exactly how the earlier bug expressed itself: two different
    requested scopes resolving to one (broader) SQL string.
    """
    rendered = {k: str(v) for k, v in _DELETE_STATEMENTS.items()}
    seen: dict[str, tuple] = {}
    for key, sql in rendered.items():
        assert sql not in seen, (
            f"combinations {seen[sql]} and {key} share the same SQL — one of "
            f"them is applying a predicate it was not asked for, or omitting one"
        )
        seen[sql] = key


# ── 2. The executed predicate set must match the requested scope ────────────

@pytest.mark.parametrize(
    "present",
    list(itertools.product((False, True), repeat=3)),
    ids=lambda p: "-".join(
        n for n, on in zip(("cid", "rid", "sk"), p) if on
    ) or "category-only",
)
def test_predicates_match_requested_scope_exactly(present):
    """For all 8 combinations: predicates == doc_category + one per argument.

    This is the test that fails on the six-entry table (for the two
    change_id+repo_id cases) and passes on the complete one.
    """
    has_cid, has_rid, has_sk = present
    kwargs = {
        "change_id": "CH-1" if has_cid else None,
        "repo_id": "R-1" if has_rid else None,
        "source_key": "SK-1" if has_sk else None,
    }

    db = _SpySession()
    _delete_chunks(db, doc_category="kb", **kwargs)

    expected = {"doc_category = :cat"}
    for name, predicate in _SCOPES.items():
        if kwargs[name] is not None:
            expected.add(predicate)

    actual = _predicates(db.statement)
    assert actual == expected, (
        f"scope {kwargs} produced predicates {sorted(actual)}, expected "
        f"{sorted(expected)}. A MISSING predicate means the DELETE is broader "
        f"than requested (data loss); an EXTRA one means it is narrower."
    )


@pytest.mark.parametrize(
    "present",
    list(itertools.product((False, True), repeat=3)),
    ids=lambda p: "-".join(
        n for n, on in zip(("cid", "rid", "sk"), p) if on
    ) or "category-only",
)
def test_bound_params_match_the_predicates(present):
    """Every predicate has a bound value, and no unused values are bound.

    An unbound placeholder is a runtime error; a bound-but-unused value is a
    sign the dispatch key and the param dict have drifted apart.
    """
    has_cid, has_rid, has_sk = present
    kwargs = {
        "change_id": "CH-1" if has_cid else None,
        "repo_id": "R-1" if has_rid else None,
        "source_key": "SK-1" if has_sk else None,
    }

    db = _SpySession()
    _delete_chunks(db, doc_category="kb", **kwargs)

    expected_keys = {"cat"}
    for name, short in (("change_id", "cid"), ("repo_id", "rid"), ("source_key", "sk")):
        if kwargs[name] is not None:
            expected_keys.add(short)

    assert set(db.params) == expected_keys

    # Every :placeholder in the SQL must have a value bound for it.
    for short in expected_keys:
        assert f":{short}" in db.statement


# ── 3. End-to-end row behaviour against a real database ─────────────────────

@pytest.fixture()
def rows(db_session):
    """A table spanning every combination of the scope columns."""
    db_session.execute(text(
        "CREATE TABLE document_chunks ("
        " id TEXT PRIMARY KEY, doc_category TEXT, change_id TEXT,"
        " repo_id TEXT, source_key TEXT)"
    ))
    made = []
    n = 0
    for cid in (None, "CH-1", "CH-2"):
        for rid in (None, "R-1", "R-2"):
            for sk in (None, "SK-1", "SK-2"):
                n += 1
                rid_ = f"id{n}"
                made.append((rid_, cid, rid, sk))
                db_session.execute(
                    text("INSERT INTO document_chunks VALUES"
                         " (:i, 'kb', :c, :r, :s)"),
                    {"i": rid_, "c": cid, "r": rid, "s": sk},
                )
    db_session.commit()
    return made


@pytest.mark.parametrize(
    "present",
    list(itertools.product((False, True), repeat=3)),
    ids=lambda p: "-".join(
        n for n, on in zip(("cid", "rid", "sk"), p) if on
    ) or "category-only",
)
def test_only_in_scope_rows_are_deleted(db_session, rows, present):
    """The rows deleted are exactly those matching every supplied scope value.

    Computed independently in Python from the fixture, so this asserts real
    behaviour rather than mirroring the implementation's own logic.
    """
    has_cid, has_rid, has_sk = present
    want_cid = "CH-1" if has_cid else None
    want_rid = "R-1" if has_rid else None
    want_sk = "SK-1" if has_sk else None

    def in_scope(row):
        _, cid, rid, sk = row
        return (
            (want_cid is None or cid == want_cid)
            and (want_rid is None or rid == want_rid)
            and (want_sk is None or sk == want_sk)
        )

    expected_survivors = sorted(r[0] for r in rows if not in_scope(r))

    _delete_chunks(
        db_session, doc_category="kb",
        change_id=want_cid, repo_id=want_rid, source_key=want_sk,
    )
    db_session.commit()

    actual = sorted(
        r[0] for r in db_session.execute(
            text("SELECT id FROM document_chunks")
        ).fetchall()
    )
    assert actual == expected_survivors, (
        f"scope(change_id={want_cid}, repo_id={want_rid}, source_key={want_sk}) "
        f"deleted the wrong rows. Over-deletion means the DELETE was broader "
        f"than the caller requested."
    )


def test_mixed_change_and_repo_scope_is_narrow(db_session, rows):
    """The exact regression case, asserted on rows.

    change_id + repo_id together must delete ONLY rows matching BOTH. The
    six-entry table dropped the change_id predicate here and removed every
    row of the repo regardless of change.
    """
    _delete_chunks(db_session, doc_category="kb", change_id="CH-1", repo_id="R-1")
    db_session.commit()

    remaining = db_session.execute(text(
        "SELECT id FROM document_chunks WHERE repo_id = 'R-1'"
    )).fetchall()

    # Rows in R-1 that belong to a DIFFERENT change must survive.
    survivors = db_session.execute(text(
        "SELECT COUNT(*) FROM document_chunks "
        "WHERE repo_id = 'R-1' AND (change_id IS NULL OR change_id != 'CH-1')"
    )).scalar()
    assert survivors > 0, (
        "every row in repo R-1 was deleted — the change_id predicate was "
        "dropped, reproducing the original over-deletion bug"
    )
    # And nothing matching both remains.
    assert not db_session.execute(text(
        "SELECT id FROM document_chunks "
        "WHERE repo_id = 'R-1' AND change_id = 'CH-1'"
    )).fetchall()
    assert remaining
