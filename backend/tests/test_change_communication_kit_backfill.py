# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""A same-version retransmit must not discard documents we do not yet hold.

The authority can communicate a change BEFORE its product kit exists and ship
the kit later — a first kit bumps no version, so the retransmit arrives at v1
against a change already stored at v1. Treating that as a pure duplicate drops
every document while the authority's UI reports "Ship N items" as a success.

The failure is completely silent on both sides. The authority sees a 202; this
platform logs "duplicate skipped"; the operator sees nothing. It only surfaces
much later, and misleadingly, when the feasibility analyser refuses with
"no change documents supplied" — a message that names neither the cause nor the
sender. Observed live: one change sat on 0 documents while its siblings held
8-9, and re-shipping could never repair it.

The guard has to stay narrow. A retransmit of documents we ALREADY hold is a
genuine duplicate and must still skip, or every redelivery re-runs the
post-receive work.
"""
from __future__ import annotations

import uuid

import pytest

from app.a2a_common.handlers.change_communication import handle_change_communication
from app.models import ChangeDocument, IncomingChange


class _Body:
    """Minimal stand-in for TaskReceiveRequest."""

    def __init__(self, change_id: str, payload: dict):
        self.change_id = change_id
        self.payload = payload
        self.correlation_id = None
        self.message_id = str(uuid.uuid4())


def _payload(change_id: str, *, docs: list | None = None, version: int = 1) -> dict:
    return {
        "change_id": change_id,
        "title": "Add hangarLocation validation",
        "kit_version": version,
        "product_kit": docs if docs is not None else [],
    }


def _doc(doc_type: str) -> dict:
    return {"doc_type": doc_type, "content": f"# {doc_type}\n\nbody"}


@pytest.fixture
def db(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401
    from app.database import Base

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    # The handler schedules post-receive work in a thread; keep the test hermetic.
    monkeypatch.setattr(
        "app.a2a_common.handlers.change_communication.schedule_post_receive",
        lambda *a, **k: None, raising=False,
    )
    try:
        yield session
    finally:
        session.close()


def _docs_for(db, change_id: str) -> int:
    return db.query(ChangeDocument).filter(ChangeDocument.change_id == change_id).count()


def test_kit_shipped_after_the_change_is_backfilled(db):
    """The regression. Change first, kit later, same version."""
    cid = str(uuid.uuid4())

    handle_change_communication(_Body(cid, _payload(cid, docs=[])), db)
    local = db.query(IncomingChange).filter_by(authority_change_id=cid).one()
    assert _docs_for(db, local.id) == 0, "precondition: no documents yet"

    handle_change_communication(
        _Body(cid, _payload(cid, docs=[_doc("circular"), _doc("faq")])), db)

    assert _docs_for(db, local.id) == 2, (
        "documents in a same-version retransmit were discarded — the authority "
        "reports a successful ship and this platform holds nothing")


def test_true_duplicate_still_skips(db):
    """Narrowness guard: redelivering documents we hold must remain a no-op."""
    cid = str(uuid.uuid4())
    docs = [_doc("circular"), _doc("faq")]

    handle_change_communication(_Body(cid, _payload(cid, docs=docs)), db)
    local = db.query(IncomingChange).filter_by(authority_change_id=cid).one()
    assert _docs_for(db, local.id) == 2

    result = handle_change_communication(_Body(cid, _payload(cid, docs=docs)), db)

    assert "duplicate skipped" in (result.get("message") or "")
    assert _docs_for(db, local.id) == 2, "a duplicate retransmit multiplied the rows"


def test_empty_retransmit_against_a_stocked_change_skips(db):
    """An empty payload must never be read as 'delete what you have'."""
    cid = str(uuid.uuid4())
    handle_change_communication(_Body(cid, _payload(cid, docs=[_doc("circular")])), db)
    local = db.query(IncomingChange).filter_by(authority_change_id=cid).one()

    result = handle_change_communication(_Body(cid, _payload(cid, docs=[])), db)

    assert "duplicate skipped" in (result.get("message") or "")
    assert _docs_for(db, local.id) == 1


def test_higher_version_still_takes_the_revision_path(db):
    """The backfill branch must not swallow a genuine revision."""
    cid = str(uuid.uuid4())
    handle_change_communication(_Body(cid, _payload(cid, docs=[_doc("circular")])), db)
    local = db.query(IncomingChange).filter_by(authority_change_id=cid).one()

    handle_change_communication(
        _Body(cid, _payload(cid, docs=[_doc("circular")], version=2)), db)

    db.refresh(local)
    assert local.negotiation_version == 2
    assert local.decision == "pending", "a new kit version must reset the decision"
