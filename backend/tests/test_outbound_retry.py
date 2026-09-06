# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the outbound A2A retry sweep (Finding 12:
security_architecture_skills.md §5.4/§11.3, EA_Skills.md P7 "DLQ and replay
process")."""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import OutboundA2ARetry, PartnerSetting
from app.services import outbound_retry


def _as_utc(value: datetime) -> datetime:
    """Normalise a value read back from the DB to an aware UTC datetime.

    Repo convention: `models._now()` writes timezone-AWARE UTC values into
    naive `DateTime` columns, so on read-back (and on SQLite in particular)
    they come back naive. Comparing those directly against
    `datetime.now(timezone.utc)` raises TypeError. The stored instant is UTC
    by construction, so re-attaching the tzinfo is the correct normalisation
    rather than a workaround.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _seed_settings(db_session):
    db_session.add(PartnerSetting(key="npci_a2a_url", value="http://npci_backend:8000"))
    db_session.commit()


def _add_row(db_session, *, next_retry_at=None, attempts=0, status="pending"):
    row = OutboundA2ARetry(
        change_id="c1",
        task_type="query",
        payload={"message": "hi"},
        attempts=attempts,
        status=status,
        next_retry_at=next_retry_at or (datetime.now(timezone.utc) - timedelta(minutes=1)),
    )
    db_session.add(row)
    db_session.commit()
    return row


class TestEnqueue:
    def test_creates_a_pending_row(self, db_session):
        outbound_retry.enqueue(
            db_session, change_id="c1", task_type="query",
            payload={"message": "hi"}, error="boom",
        )
        row = db_session.query(OutboundA2ARetry).one()
        assert row.status == "pending"
        assert row.attempts == 0
        assert row.last_error == "boom"
        assert _as_utc(row.next_retry_at) > datetime.now(timezone.utc)

    def test_carries_correlation_id(self, db_session):
        outbound_retry.enqueue(
            db_session, change_id="c1", task_type="query",
            payload={}, error="boom", correlation_id="job-1",
        )
        row = db_session.query(OutboundA2ARetry).one()
        assert row.correlation_id == "job-1"

    def test_truncates_long_error_messages(self, db_session):
        outbound_retry.enqueue(
            db_session, change_id="c1", task_type="query",
            payload={}, error="x" * 10_000,
        )
        row = db_session.query(OutboundA2ARetry).one()
        assert len(row.last_error) == 500


class TestRunSweepDelivery:
    def test_delivers_due_row_and_marks_delivered(self, db_session, monkeypatch):
        _seed_settings(db_session)
        _add_row(db_session)

        # ITA-3: the transport core is async; the success-path fake must be a
        # coroutine function or the sweep's portable bridge has nothing to run.
        async def fake_dispatch(*a, **kw):
            return None

        monkeypatch.setattr("app.npci_client._dispatch_wire", fake_dispatch)
        counts = outbound_retry.run_sweep(db_session, max_attempts=6)

        assert counts == {"delivered": 1, "requeued": 0, "abandoned": 0}
        row = db_session.query(OutboundA2ARetry).one()
        assert row.status == "delivered"
        assert row.attempts == 1

    def test_ignores_rows_not_yet_due(self, db_session, monkeypatch):
        _seed_settings(db_session)
        _add_row(db_session, next_retry_at=datetime.now(timezone.utc) + timedelta(hours=1))

        monkeypatch.setattr("app.npci_client._dispatch_wire", lambda *a, **kw: None)
        counts = outbound_retry.run_sweep(db_session, max_attempts=6)

        assert counts == {"delivered": 0, "requeued": 0, "abandoned": 0}
        row = db_session.query(OutboundA2ARetry).one()
        assert row.status == "pending"
        assert row.attempts == 0

    def test_ignores_rows_not_pending(self, db_session, monkeypatch):
        _seed_settings(db_session)
        _add_row(db_session, status="delivered")
        _add_row(db_session, status="abandoned")

        monkeypatch.setattr("app.npci_client._dispatch_wire", lambda *a, **kw: None)
        counts = outbound_retry.run_sweep(db_session, max_attempts=6)

        assert counts == {"delivered": 0, "requeued": 0, "abandoned": 0}


class TestRunSweepRequeue:
    def test_failed_attempt_requeues_with_backoff(self, db_session, monkeypatch):
        _seed_settings(db_session)
        _add_row(db_session)

        def _fail(*a, **kw):
            raise RuntimeError("still down")

        monkeypatch.setattr("app.npci_client._dispatch_wire", _fail)
        counts = outbound_retry.run_sweep(db_session, max_attempts=6)

        assert counts == {"delivered": 0, "requeued": 1, "abandoned": 0}
        row = db_session.query(OutboundA2ARetry).one()
        assert row.status == "pending"
        assert row.attempts == 1
        # `last_error` is rendered in the retry-queue UI, so npci_client records
        # the exception TYPE via safe_exc() rather than str(exc) — an httpx or
        # auth message would otherwise pin the resolved NPCI host, port and token
        # prefix into a user-visible row (CWE-209). Asserting the type name, and
        # asserting the detail is absent, makes this a guard FOR that redaction
        # instead of against it.
        assert row.last_error == "RuntimeError"
        assert "still down" not in (row.last_error or "")
        assert _as_utc(row.next_retry_at) > datetime.now(timezone.utc)

    def test_backoff_grows_across_repeated_failures(self, db_session, monkeypatch):
        _seed_settings(db_session)
        _add_row(db_session)

        def _fail(*a, **kw):
            raise RuntimeError("still down")

        monkeypatch.setattr("app.npci_client._dispatch_wire", _fail)

        before = datetime.now(timezone.utc)
        outbound_retry.run_sweep(db_session, max_attempts=10)
        row = db_session.query(OutboundA2ARetry).one()
        first_wait = (_as_utc(row.next_retry_at) - before).total_seconds()

        # Force it due again and fail a second time — backoff must have grown.
        row.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db_session.commit()
        before2 = datetime.now(timezone.utc)
        outbound_retry.run_sweep(db_session, max_attempts=10)
        db_session.refresh(row)
        second_wait = (_as_utc(row.next_retry_at) - before2).total_seconds()

        assert second_wait > first_wait


class TestRunSweepAbandonment:
    def test_exhausted_attempts_marks_abandoned_and_emits_event(self, db_session, monkeypatch, caplog):
        _seed_settings(db_session)
        _add_row(db_session, attempts=5)  # one more failure hits max_attempts=6

        def _fail(*a, **kw):
            raise RuntimeError("permanently down")

        monkeypatch.setattr("app.npci_client._dispatch_wire", _fail)

        events = []
        monkeypatch.setattr(
            "app.core.security_events.emit_security_event",
            lambda **kw: events.append(kw),
        )

        counts = outbound_retry.run_sweep(db_session, max_attempts=6)

        assert counts == {"delivered": 0, "requeued": 0, "abandoned": 1}
        row = db_session.query(OutboundA2ARetry).one()
        assert row.status == "abandoned"
        assert row.attempts == 6

        assert len(events) == 1
        assert events[0]["event_name"] == "outbound_a2a_delivery_abandoned"
        assert events[0]["severity"] == "high"

    def test_reads_default_max_attempts_from_settings(self, db_session, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "outbound_retry_max_attempts", 1)
        _seed_settings(db_session)
        _add_row(db_session, attempts=0)

        def _fail(*a, **kw):
            raise RuntimeError("down")

        monkeypatch.setattr("app.npci_client._dispatch_wire", _fail)
        counts = outbound_retry.run_sweep(db_session)  # no explicit max_attempts

        assert counts == {"delivered": 0, "requeued": 0, "abandoned": 1}
