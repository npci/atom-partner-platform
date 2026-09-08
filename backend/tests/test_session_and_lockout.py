# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Two auth controls that were present but not doing their job.

1. The login lockout keyed a tier on `request.client.host`. Behind the shipped
   edge that is the nginx CONTAINER — the same address for every operator — so
   the tier was one shared bucket. Ten failed logins from an anonymous caller
   locked out the whole platform, and because the lockout is checked BEFORE
   `verify_password`, no legitimate login could clear it.

2. Changing a password left every existing JWT valid for the rest of its 24h
   life, so the standard containment step after a stolen cookie contained
   nothing.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.api import auth as auth_mod


class _Req:
    """Minimal stand-in for the parts of Request that _client_ip reads."""

    def __init__(self, peer, headers=None):
        self.client = type("C", (), {"host": peer})() if peer else None
        self.headers = headers or {}


@pytest.fixture(autouse=True)
def _clear_lockout_state():
    with auth_mod._failures_lock:
        auth_mod._login_failures.clear()
    yield
    with auth_mod._failures_lock:
        auth_mod._login_failures.clear()


class TestClientIpAttribution:
    def test_no_proxy_uses_the_peer(self, monkeypatch):
        monkeypatch.setattr(auth_mod.settings, "partner_trusted_proxy_hops", 0, raising=False)
        assert auth_mod._client_ip(_Req("203.0.113.7")) == "203.0.113.7"

    def test_no_proxy_ignores_a_forged_forwarded_header(self, monkeypatch):
        """With no declared proxy the header is attacker-supplied noise."""
        monkeypatch.setattr(auth_mod.settings, "partner_trusted_proxy_hops", 0, raising=False)
        req = _Req("203.0.113.7", {"x-forwarded-for": "1.2.3.4"})
        assert auth_mod._client_ip(req) == "203.0.113.7"

    def test_one_hop_reads_the_real_client(self, monkeypatch):
        monkeypatch.setattr(auth_mod.settings, "partner_trusted_proxy_hops", 1, raising=False)
        req = _Req("172.18.0.5", {"x-forwarded-for": "203.0.113.7"})
        assert auth_mod._client_ip(req) == "203.0.113.7"

    def test_one_hop_takes_the_rightmost_trusted_entry(self, monkeypatch):
        """A client that pre-seeds X-Forwarded-For must not pick its own key.

        nginx APPENDS, so with one proxy the last entry is what our edge saw.
        Reading the leftmost value instead would let a caller rotate the bucket
        per request and evade the lockout entirely.
        """
        monkeypatch.setattr(auth_mod.settings, "partner_trusted_proxy_hops", 1, raising=False)
        req = _Req("172.18.0.5", {"x-forwarded-for": "9.9.9.9, 203.0.113.7"})
        assert auth_mod._client_ip(req) == "203.0.113.7"

    def test_missing_header_behind_a_proxy_is_unattributable(self, monkeypatch):
        """The request bypassed the edge — docker-compose.override.yml
        publishes the backend on loopback, so this really happens. Returning
        None makes the caller SKIP the IP tier rather than guess."""
        monkeypatch.setattr(auth_mod.settings, "partner_trusted_proxy_hops", 1, raising=False)
        assert auth_mod._client_ip(_Req("127.0.0.1")) is None

    def test_short_chain_is_unattributable(self, monkeypatch):
        monkeypatch.setattr(auth_mod.settings, "partner_trusted_proxy_hops", 2, raising=False)
        req = _Req("172.18.0.5", {"x-forwarded-for": "203.0.113.7"})
        assert auth_mod._client_ip(req) is None


class TestSharedBucketCannotLockEveryoneOut:
    def test_unattributable_ip_does_not_create_a_shared_key(self, monkeypatch):
        """The regression itself: with no usable client address, failures must
        not accumulate under one key that every operator also checks."""
        monkeypatch.setattr(auth_mod.settings, "partner_trusted_proxy_hops", 1, raising=False)
        assert auth_mod._client_ip(_Req("172.18.0.5")) is None

        # Simulate what login does with that verdict.
        for _ in range(20):
            auth_mod._record_failure("user:attacker-picked-name")

        # A different operator's key is untouched, and there is no "ip:None"
        # or "ip:unknown" bucket standing in for everybody.
        auth_mod._check_lockout("user:realoperator")
        assert not [k for k in auth_mod._login_failures if k.startswith("ip:")]

    def test_per_username_lockout_still_fires(self):
        """The tier that actually defends a credential must be unaffected."""
        for _ in range(auth_mod._LOCKOUT_TIER_1[0]):
            auth_mod._record_failure("user:victim")
        with pytest.raises(auth_mod.HTTPException) as exc:
            auth_mod._check_lockout("user:victim")
        assert exc.value.status_code == 429


class TestPasswordChangeRetiresSessions:
    """`get_current_user` rejects a token issued before password_changed_at."""

    def _decode_ok(self, db_session, token):
        from app.api.auth import get_current_user

        class _CaseInsensitive(dict):
            """Starlette's Headers is case-insensitive; a plain dict is not.

            The first version of this helper used a plain dict keyed
            "authorization" while `_extract_token` looks up "Authorization",
            so no token was ever presented — and the rejection test passed on
            a 401 "Not authenticated" instead of on the cut-off it was written
            to prove. Hence the explicit detail assertions below.
            """

            def get(self, key, default=None):
                for k, v in self.items():
                    if k.lower() == key.lower():
                        return v
                return default

        class _R:
            headers = _CaseInsensitive({"Authorization": f"Bearer {token}"})
            cookies = {}
        return get_current_user(_R(), db_session)

    def _user(self, db_session, **kw):
        from app.api.auth import hash_password
        from app.models import PartnerUser

        u = PartnerUser(
            username=kw.get("username", "op1"),
            password_hash=hash_password("Str0ngPass1"),
            role="admin", is_active=True,
            password_changed_at=kw.get("password_changed_at"),
        )
        db_session.add(u)
        db_session.commit()
        return u

    def test_token_issued_before_the_cutoff_is_rejected(self, db_session):
        from fastapi import HTTPException

        user = self._user(db_session)
        token = auth_mod.create_token(user)
        # Rotate the password "after" the token was minted.
        user.password_changed_at = (
            datetime.now(timezone.utc) + timedelta(seconds=5)
        ).replace(microsecond=0, tzinfo=None)
        db_session.commit()

        with pytest.raises(HTTPException) as exc:
            self._decode_ok(db_session, token)
        assert exc.value.status_code == 401
        # Specifically the cut-off rejection, not "no token was presented" —
        # both are 401 and only one of them proves anything.
        assert exc.value.detail == "Invalid or expired token"

    def test_token_issued_after_the_cutoff_is_accepted(self, db_session):
        user = self._user(db_session)
        user.password_changed_at = (
            datetime.now(timezone.utc) - timedelta(seconds=5)
        ).replace(microsecond=0, tzinfo=None)
        db_session.commit()
        token = auth_mod.create_token(user)
        assert self._decode_ok(db_session, token).username == user.username

    def test_no_cutoff_keeps_existing_sessions_working(self, db_session):
        """NULL means "never rotated" — the upgrade must not log everyone out."""
        user = self._user(db_session, password_changed_at=None)
        token = auth_mod.create_token(user)
        assert self._decode_ok(db_session, token).username == user.username

    def test_token_minted_in_the_same_second_survives(self, db_session):
        """The caller's own replacement cookie must not be invalidated by the
        cut-off set microseconds earlier — `iat` has second resolution."""
        user = self._user(db_session)
        user.password_changed_at = datetime.now(timezone.utc).replace(
            microsecond=0, tzinfo=None)
        db_session.commit()
        token = auth_mod.create_token(user)
        assert self._decode_ok(db_session, token).username == user.username
