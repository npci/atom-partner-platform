# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Outbound A2A retries must be idempotent (EA_Skills.md P3 — "Idempotent
operations and safe retries"; Critical example "non-idempotent payment
processing with retry paths").

The envelope `message_id` is the receiver's dedup key. Before this, every
attempt minted a fresh uuid4, so a send NPCI actually processed but whose ACK
was lost came back through the retry sweep looking like a brand-new message
and was applied twice.
"""
import pytest

from app.models import OutboundA2ARetry, PartnerSetting
from app.services import outbound_retry


def _seed(db_session):
    db_session.add(PartnerSetting(key="npci_a2a_url", value="http://npci_backend:8000"))
    db_session.commit()


class TestKeyPersisted:
    def test_enqueue_stores_the_supplied_key(self, db_session):
        outbound_retry.enqueue(
            db_session, change_id="c1", task_type="query", payload={"m": "hi"},
            error="boom", idempotency_key="fixed-key-123",
        )
        row = db_session.query(OutboundA2ARetry).one()
        assert row.idempotency_key == "fixed-key-123"

    def test_enqueue_generates_a_key_when_none_supplied(self, db_session):
        """Even without a caller-supplied key, retries of THIS row must be
        mutually idempotent — so a key is generated rather than left NULL."""
        outbound_retry.enqueue(
            db_session, change_id="c1", task_type="query", payload={"m": "hi"}, error="boom",
        )
        row = db_session.query(OutboundA2ARetry).one()
        assert row.idempotency_key


class TestKeyReusedAcrossAttempts:
    def test_sweep_resends_under_the_original_key(self, db_session, monkeypatch):
        _seed(db_session)
        outbound_retry.enqueue(
            db_session, change_id="c1", task_type="query", payload={"m": "hi"},
            error="boom", idempotency_key="original-key",
        )
        row = db_session.query(OutboundA2ARetry).one()
        row.next_retry_at = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ) - __import__("datetime").timedelta(minutes=1)
        db_session.commit()

        seen = {}

        def fake_dispatch(db, task_type, change_id, payload, *, job_correlation_id=None,
                          idempotency_key=None):
            seen["key"] = idempotency_key

        import app.npci_client as npci
        monkeypatch.setattr(npci, "_dispatch_wire", fake_dispatch)

        outbound_retry.run_sweep(db_session)
        assert seen["key"] == "original-key", (
            "the retry minted a new id — NPCI cannot deduplicate it"
        )

    def test_repeated_failures_keep_the_same_key(self, db_session, monkeypatch):
        """Across multiple failed attempts the key must never rotate."""
        import datetime as dt

        _seed(db_session)
        outbound_retry.enqueue(
            db_session, change_id="c1", task_type="query", payload={"m": "hi"},
            error="boom", idempotency_key="stable-key",
        )

        keys = []

        def failing_dispatch(db, task_type, change_id, payload, *, job_correlation_id=None,
                             idempotency_key=None):
            keys.append(idempotency_key)
            raise RuntimeError("still down")

        import app.npci_client as npci
        monkeypatch.setattr(npci, "_dispatch_wire", failing_dispatch)

        for _ in range(3):
            row = db_session.query(OutboundA2ARetry).one()
            row.next_retry_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
            db_session.commit()
            outbound_retry.run_sweep(db_session)

        assert len(keys) == 3
        assert set(keys) == {"stable-key"}


class TestDispatchWireHonoursTheKey:
    def test_dispatch_wire_uses_supplied_key_as_message_id(self, monkeypatch):
        """The key must land in the envelope's message_id — that is the field
        the receiver deduplicates on."""
        import app.npci_client as npci

        captured = {}

        monkeypatch.setattr(npci, "authenticate", lambda db: "tok")
        monkeypatch.setattr(npci, "_get_a2a_base_url", lambda db: "https://npci.example")
        monkeypatch.setattr(npci, "_validate_url_scheme", lambda *a, **k: None)
        monkeypatch.setattr(npci, "_get_setting", lambda db, k: None)
        monkeypatch.setattr(npci, "_resolve_correlation_id", lambda db, cid, p: "corr-1")

        def fake_envelope(task_type, **kw):
            captured["message_id"] = kw.get("message_id")
            return {"ok": True}

        monkeypatch.setattr(npci, "make_envelope", fake_envelope)

        # ITA-3: `_dispatch_wire` is genuinely async (there is no inner
        # `asyncio.run` left to stub) — stub the transport coroutine and
        # await the real thing.
        async def fake_send(**kw):
            return None

        monkeypatch.setattr(npci, "send_a2a_message", fake_send)

        import asyncio

        asyncio.run(npci._dispatch_wire(
            None, "query", "c1", {"m": "hi"}, idempotency_key="key-abc",
        ))
        assert captured["message_id"] == "key-abc"
