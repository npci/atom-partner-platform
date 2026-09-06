# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The settings endpoint must refuse weak HS256 secrets (CVE-2025-45768).

`test_key_strength.py` covers the policy rules in isolation. This file proves the
policy is actually WIRED IN at the HTTP boundary — the two are different failures,
and only the second one protects production.

Why the boundary matters more than the unit here: `npci_jwt_secret` is the HS256
key that `PartnerAuthMiddleware` verifies every inbound A2A call against, and
`npci_hmac_secret` drives request signing. Neither can be checked at startup
because both live in `partner_settings` and are installed at runtime through
`PUT /api/settings`. If that endpoint accepts a weak value, the control does not
exist regardless of how well the validator behaves in a unit test.
"""
from __future__ import annotations

import secrets

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

import app.database as database
from app.api.auth import get_current_user, require_admin
from app.database import get_db
from app.main import app
from app.models import Base, PartnerSetting, PartnerUser


@pytest.fixture()
def client(monkeypatch):
    # A valid KEK so `secret_box.encrypt` works on the write path — without it
    # the endpoint would fail for an unrelated reason and these tests would pass
    # vacuously.
    import base64

    monkeypatch.setenv(
        "PARTNER_SECRET_KEK",
        base64.b64encode(secrets.token_bytes(32)).decode(),
    )

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    database.engine = engine
    database.SessionLocal.configure(bind=engine)
    Base.metadata.create_all(engine)

    def _override_db():
        db = database.SessionLocal()
        try:
            yield db
        finally:
            db.close()

    admin = PartnerUser(
        id="u-admin", username="admin", password_hash="x",
        full_name="Admin", role="admin", is_active=True,
    )
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[require_admin] = lambda: admin
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)


# Deliberately spans several rejection rules: too short, padded, placeholder
# text, and low distinct-character count.
_WEAK = ["short", "secret", "a" * 32, "changeme-changeme-changeme-changeme"]


@pytest.mark.parametrize("field", ["npci_jwt_secret", "npci_hmac_secret"])
@pytest.mark.parametrize("weak", _WEAK)
def test_weak_secret_is_rejected_with_400(client, field, weak):
    """A weak value must be refused, and must NOT be persisted.

    Both assertions matter. A 400 that still wrote the row would be worse than
    no check at all: the operator sees an error, assumes nothing was saved, and
    the weak key is live on the A2A ingress.
    """
    resp = client.put("/api/settings", json={field: weak})
    assert resp.status_code == 400, resp.text

    detail = resp.json()["detail"]
    assert "CVE-2025-45768" in detail

    # ── The response must not disclose the secret ────────────────────────────
    # This body travels back over HTTP and can land in a proxy or browser log,
    # so it must describe the SHAPE of the problem, not the value.
    #
    # Stated precisely: no fragment of the rejected value may appear, EXCEPT a
    # placeholder token that the validator deliberately quotes back ("contains
    # the placeholder text 'secret'"). Those tokens are fixed constants from a
    # public list in `key_strength._PLACEHOLDER_TOKENS` — they carry no
    # information about the operator's input beyond "it contains a well-known
    # bad word", which is the whole point of the message. Anything else
    # appearing would be real leakage.
    from app.core.key_strength import _PLACEHOLDER_TOKENS

    residual = detail
    for token in _PLACEHOLDER_TOKENS:
        residual = residual.replace(token, "")
    # Also drop the label, which legitimately contains the word "secret".
    for label_word in ("NPCI JWT secret", "NPCI HMAC secret", "secret"):
        residual = residual.replace(label_word, "")

    # Any run of >= 6 characters from the rejected value must be gone.
    for i in range(len(weak) - 5):
        fragment = weak[i:i + 6]
        assert fragment not in residual, (
            f"the 400 response leaked the fragment {fragment!r} of the "
            f"rejected secret"
        )

    # Nothing was written.
    db = database.SessionLocal()
    try:
        assert db.get(PartnerSetting, field) is None
    finally:
        db.close()


@pytest.mark.parametrize("field", ["npci_jwt_secret", "npci_hmac_secret"])
def test_strong_secret_is_accepted_and_persisted(client, field):
    """The happy path must still work — a control that blocks valid NPCI-issued
    secrets would break partner onboarding, which is the failure mode this whole
    change most needs to avoid."""
    strong = secrets.token_urlsafe(48)
    resp = client.put("/api/settings", json={field: strong})
    assert resp.status_code == 200, resp.text
    assert field in resp.json()["persisted"]

    db = database.SessionLocal()
    try:
        row = db.get(PartnerSetting, field)
        assert row is not None
        # Stored encrypted at rest, never as cleartext.
        assert row.value != strong
        assert row.value.startswith("enc:v1:")
    finally:
        db.close()


def test_empty_value_still_means_leave_unchanged(client):
    """An omitted/empty secret must not be treated as a weak one.

    Empty means "leave the existing value alone" in this API. If the strength
    check fired on empty, every settings save that only changed a URL would
    fail — the classic way a security control gets reverted wholesale.
    """
    resp = client.put("/api/settings", json={"partner_name": "HDFC"})
    assert resp.status_code == 200, resp.text
    assert "npci_jwt_secret" not in resp.json()["persisted"]


def test_rejection_does_not_partially_persist_other_fields(client):
    """One weak secret must not leave a half-applied settings update.

    The handler validates and writes field by field, so a rejection midway
    could otherwise commit the fields processed before it. Here `partner_name`
    is written before the JWT secret is validated: the request must fail and
    the name must not be saved.
    """
    resp = client.put(
        "/api/settings",
        json={"partner_name": "ShouldNotStick", "npci_jwt_secret": "weak"},
    )
    assert resp.status_code == 400, resp.text

    db = database.SessionLocal()
    try:
        row = db.get(PartnerSetting, "partner_name")
        assert row is None or row.value != "ShouldNotStick", (
            "a rejected settings update partially persisted — the weak-secret "
            "rejection must abort the whole transaction"
        )
    finally:
        db.close()
