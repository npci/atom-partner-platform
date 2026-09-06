# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""PTNR-F37 — the certification result channel must be authenticated.

`POST /integration-testing/cert-case-outcome` is how a partner-executed case's
verdict reaches the authority: this platform forwards it as
`cert_case_result(reporter="bank")`, and the authority maps the reported status
string straight onto the run's result row without re-grading it. `cert_join`
then certifies on `failed == 0`.

So an unauthenticated caller on this port does not need to defeat HMAC or JWT —
it makes THIS platform, holding its own valid credentials, assert a pass nobody
authorised. The tunnel route's recorded "dev-only, the authority's allowlist is
the control that matters" exemption does not transfer here, because on this path
there is no receiving-side control to lean on.

These tests pin the three behaviours that close it, including the negative one
that matters most: a wrong token must not be admitted.
"""
import secrets as _secrets

from starlette.testclient import TestClient

from app.api import integration_testing as it_module


class _Row:
    """Stand-in for a `partner_settings` row."""

    def __init__(self, value):
        self.value = value


class _DB:
    """Minimal `db.get(PartnerSetting, key)` stub — no database needed.

    Also captures `add`/`commit`, because an admitted outcome now writes its
    own `CertCaseExecution` evidence row before forwarding.
    """

    def __init__(self, secret):
        self._secret = secret
        self.added = []
        self.commits = 0

    def get(self, _model, key):
        if key == "cert_trigger_secret" and self._secret is not None:
            return _Row(self._secret)
        return None

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.commits += 1


def _client(monkeypatch, secret, *, enabled=True):
    """A TestClient over just this router, with the settings/DB seams stubbed.

    `settings` is replaced via monkeypatch, NOT by assigning to the module
    attribute directly: a bare assignment persists after the test and leaks
    into every later test that imports this module, which is a test-order
    dependent failure of exactly the kind this suite exists to catch.
    """
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(it_module.router)
    db = _DB(secret)
    app.dependency_overrides[it_module.get_db] = lambda: db

    class _S:
        integration_testing_enabled = enabled

    monkeypatch.setattr(it_module, "settings", _S())
    client = TestClient(app)
    # The stub is reachable from the test so the evidence write can be asserted
    # on, not merely tolerated.
    client.db = db
    return client


_BODY = {
    "npci_change_id": "f989926b-2dd3-40d9-955b-a07bfca89b89",
    "case_id": "LL_1",
    "status": "passed",
}


def test_unauthenticated_outcome_is_refused(monkeypatch):
    """The regression that motivated this file: no credential, no verdict.

    Before the fix this returned 422 from the handler's own validator, proving
    it had executed with nothing in front of it.
    """
    secret = f"rig-{_secrets.token_urlsafe(32)}"
    resp = _client(monkeypatch, secret).post("/integration-testing/cert-case-outcome", json=_BODY)
    assert resp.status_code == 401
    assert "credential" in resp.json()["error"]


def test_wrong_token_is_refused(monkeypatch):
    """A present-but-wrong bearer must fail exactly like an absent one.

    Guards against a check that merely tests for the header's presence.
    """
    secret = f"rig-{_secrets.token_urlsafe(32)}"
    other = f"rig-{_secrets.token_urlsafe(32)}"
    resp = _client(monkeypatch, secret).post(
        "/integration-testing/cert-case-outcome",
        json=_BODY,
        headers={"Authorization": f"Bearer {other}"},
    )
    assert resp.status_code == 401


def test_unconfigured_secret_fails_closed(monkeypatch):
    """No `cert_trigger_secret` configured ⇒ the channel is CLOSED, not open.

    Fail-open here would restore the whole defect on any deployment that simply
    never set the value — which is the state every fresh install starts in.
    """
    resp = _client(monkeypatch, None).post("/integration-testing/cert-case-outcome", json=_BODY)
    assert resp.status_code == 503
    assert "not configured" in resp.json()["error"]


def test_correct_token_is_admitted_and_forwards(monkeypatch):
    """The positive control: the rig's real token still works end to end.

    An over-eager guard that refused everything would 'fix' the defect by
    breaking certification, which is the failure mode worth pinning against.
    """
    secret = f"rig-{_secrets.token_urlsafe(32)}"
    sent = {}

    def _fake_send(db, change_id, case_id, status, attempt=1, details=None,
                   reporter=None):
        sent.update(change_id=change_id, case_id=case_id, status=status,
                    reporter=reporter)
        return {"ok": True}

    import app.npci_client as npci_client

    monkeypatch.setattr(npci_client, "send_cert_case_result", _fake_send)

    client = _client(monkeypatch, secret)
    resp = client.post(
        "/integration-testing/cert-case-outcome",
        json=_BODY,
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert resp.status_code == 202
    assert resp.json()["forwarded"] is True
    # reporter="bank" is what makes the authority attach it to the run at all.
    assert sent["reporter"] == "bank"
    assert sent["case_id"] == "LL_1"
    # ...and this platform kept its OWN evidence row for the case.
    assert len(client.db.added) == 1
    assert client.db.added[0].case_id == "LL_1"


def test_evidence_is_persisted_even_when_the_forward_fails(monkeypatch):
    """The reason the write happens BEFORE the send, pinned.

    The authority's deadline sweep covers a report that never crosses the wire;
    nothing covers evidence this side never kept. If the order were reversed, a
    send failure would erase the only local record of what the application
    answered and how it was graded — precisely the round an operator most needs
    to inspect.
    """
    secret = f"rig-{_secrets.token_urlsafe(32)}"

    def _boom(*_a, **_kw):
        raise RuntimeError("authority unreachable")

    import app.npci_client as npci_client

    monkeypatch.setattr(npci_client, "send_cert_case_result", _boom)

    client = _client(monkeypatch, secret)
    try:
        client.post("/integration-testing/cert-case-outcome", json=_BODY,
                    headers={"Authorization": f"Bearer {secret}"})
    except RuntimeError:
        pass  # the send blew up; the question is what survived it

    assert len(client.db.added) == 1
    assert client.db.added[0].case_id == "LL_1"
    assert client.db.commits >= 1
