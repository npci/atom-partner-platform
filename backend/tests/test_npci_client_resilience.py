# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for npci_client.py's outbound resilience (circuit breaker + bulkhead)
and retry-queue behavior (Finding 12: security_architecture_skills.md
§5.4/§11.3), plus correlation id propagation (Finding 13: §13.1)."""
import pytest

from app.core.correlation import use_correlation_id
from app.core.resilience import CircuitOpenError, reset_for_tests
from app.models import IncomingChange, OutboundA2ARetry, PartnerSetting
from app.npci_client import send_task


@pytest.fixture(autouse=True)
def _reset():
    reset_for_tests()
    yield
    reset_for_tests()


def _authenticated(monkeypatch):
    import app.npci_client as npci_client
    monkeypatch.setattr(npci_client, "authenticate", lambda db: "fake-jwt-token")


def _seed_settings(db_session):
    db_session.add(PartnerSetting(key="npci_a2a_url", value="http://npci_backend:8000"))
    db_session.commit()


class TestSendTaskSuccessPath:
    def test_success_returns_delivered(self, db_session, monkeypatch):
        _authenticated(monkeypatch)
        _seed_settings(db_session)

        async def _fake_send(*a, **kw):
            return None

        monkeypatch.setattr("app.npci_client.send_a2a_message", _fake_send)
        result = send_task(db_session, "query", "change-1", {"message": "hi"})
        assert result == {"status": "delivered"}
        assert db_session.query(OutboundA2ARetry).count() == 0  # nothing queued on success


class TestSendTaskFailureEnqueuesRetry:
    def test_transport_failure_enqueues_retry_row(self, db_session, monkeypatch):
        _authenticated(monkeypatch)
        _seed_settings(db_session)

        async def _failing_send(*a, **kw):
            raise RuntimeError("connection reset")

        monkeypatch.setattr("app.npci_client.send_a2a_message", _failing_send)
        result = send_task(db_session, "query", "change-1", {"message": "hi"})
        assert result is None

        rows = db_session.query(OutboundA2ARetry).all()
        assert len(rows) == 1
        assert rows[0].task_type == "query"
        assert rows[0].change_id == "change-1"
        assert rows[0].status == "pending"
        # See test_outbound_retry.py: last_error carries the exception TYPE, not
        # its message, so a transport failure cannot leak the resolved host or a
        # token prefix into the retry-queue view (CWE-209).
        assert rows[0].last_error == "RuntimeError"
        assert "connection reset" not in (rows[0].last_error or "")
        assert rows[0].next_retry_at is not None

    def test_auth_failure_enqueues_retry_row(self, db_session, monkeypatch):
        import app.npci_client as npci_client
        monkeypatch.setattr(npci_client, "authenticate", lambda db: None)  # auth fails
        _seed_settings(db_session)

        result = send_task(db_session, "query", "change-1", {"message": "hi"})
        assert result is None
        assert db_session.query(OutboundA2ARetry).count() == 1


class TestCircuitBreakerOnOutbound:
    def test_open_circuit_rejects_without_calling_transport_and_enqueues_retry(
        self, db_session, monkeypatch,
    ):
        from app.config import settings
        monkeypatch.setattr(settings, "npci_cb_failure_threshold", 2)

        _authenticated(monkeypatch)
        _seed_settings(db_session)

        call_count = {"n": 0}

        async def _failing_send(*a, **kw):
            call_count["n"] += 1
            raise RuntimeError("npci down")

        monkeypatch.setattr("app.npci_client.send_a2a_message", _failing_send)

        # Rebuild the hostility registry so the patched threshold takes effect.
        from app.core.hostility import validate_at_startup
        validate_at_startup()

        # Trip the breaker.
        send_task(db_session, "query", "c1", {"message": "1"})
        send_task(db_session, "query", "c1", {"message": "2"})
        assert call_count["n"] == 2

        # Next call must fail via the OPEN circuit, WITHOUT calling transport again.
        result = send_task(db_session, "query", "c1", {"message": "3"})
        assert result is None
        assert call_count["n"] == 2  # unchanged — breaker rejected before dispatch

        # Still enqueued for retry even though the breaker (not the transport)
        # was what rejected it — a down dependency must not silently drop mail.
        assert db_session.query(OutboundA2ARetry).count() == 3


class TestCorrelationIdPropagation:
    def test_explicit_correlation_id_sent_as_header(self, db_session, monkeypatch):
        _authenticated(monkeypatch)
        _seed_settings(db_session)

        captured = {}

        async def _capture_send(*a, **kw):
            captured.update(kw)

        monkeypatch.setattr("app.npci_client.send_a2a_message", _capture_send)
        send_task(db_session, "query", "c1", {"message": "hi"}, correlation_id="job-abc-123")
        assert captured["correlation_id"] == "job-abc-123"

    def test_falls_back_to_active_job_correlation_id(self, db_session, monkeypatch):
        _authenticated(monkeypatch)
        _seed_settings(db_session)

        captured = {}

        async def _capture_send(*a, **kw):
            captured.update(kw)

        monkeypatch.setattr("app.npci_client.send_a2a_message", _capture_send)

        with use_correlation_id("active-job-xyz"):
            send_task(db_session, "query", "c1", {"message": "hi"})

        assert captured["correlation_id"] == "active-job-xyz"

    def test_explicit_arg_wins_over_active_job_context(self, db_session, monkeypatch):
        _authenticated(monkeypatch)
        _seed_settings(db_session)

        captured = {}

        async def _capture_send(*a, **kw):
            captured.update(kw)

        monkeypatch.setattr("app.npci_client.send_a2a_message", _capture_send)

        with use_correlation_id("active-job-xyz"):
            send_task(db_session, "query", "c1", {"message": "hi"}, correlation_id="explicit-wins")

        assert captured["correlation_id"] == "explicit-wins"

    def test_enqueued_retry_carries_correlation_id_on_failure(self, db_session, monkeypatch):
        _authenticated(monkeypatch)
        _seed_settings(db_session)

        async def _failing_send(*a, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr("app.npci_client.send_a2a_message", _failing_send)

        with use_correlation_id("job-for-retry-trace"):
            send_task(db_session, "query", "c1", {"message": "hi"})

        row = db_session.query(OutboundA2ARetry).one()
        assert row.correlation_id == "job-for-retry-trace"

    def test_envelope_correlation_id_not_overridden_by_job_id(self, db_session, monkeypatch):
        """The A2A envelope's own business correlation_id (NPCI's conversation
        thread pointer) must reflect the resolved thread id, NOT the
        platform-internal AgentJob id — those are two distinct concepts
        (see npci_client._dispatch_wire's docstring)."""
        _authenticated(monkeypatch)
        _seed_settings(db_session)
        db_session.add(IncomingChange(
            id="row-1", npci_change_id="c1", title="t", correlation_id="npci-thread-999",
        ))
        db_session.commit()

        captured = {}

        async def _capture_send(*a, **kw):
            captured.update(kw)

        monkeypatch.setattr("app.npci_client.send_a2a_message", _capture_send)

        with use_correlation_id("job-internal-id"):
            send_task(db_session, "query", "c1", {"message": "hi"})

        # The envelope (kw["data"]) carries NPCI's thread id...
        assert captured["data"]["correlation_id"] == "npci-thread-999"
        # ...while the transport header carries the job's internal id.
        assert captured["correlation_id"] == "job-internal-id"
