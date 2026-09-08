# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""A2A replay protection must actually reject a replay.

`hmac_signer.verify()` skips the nonce check when handed `redis_client=None`,
and `hmac_middleware` passed None unconditionally — so `"replay_detected"` was
listed in `_KNOWN_REJECT_CODES` but was unreachable, and the only bound on
replaying a captured signed request was the 300s timestamp skew.

`test_a2a_fail_closed.py` covers the unconfigured / mismatch / oversize
branches and says nothing about replay or skew, which is why this went
unnoticed. These tests drive `hmac_signer.verify()` itself so they fail if the
store is ever disconnected again, not just if the store's own logic breaks.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.a2a_common.hmac_signer import sign, verify
from app.a2a_common.nonce_store_db import DbNonceStore

_BODY = b'{"jsonrpc":"2.0","method":"message/send","params":{}}'


@pytest.fixture()
def secret():
    """Generated per test, never a literal.

    `test_no_hardcoded_secret_literals` rejects a credential-shaped literal in
    this directory even in test code — static analysis reads one as a hardcoded
    password regardless of context. It caught the first draft of this file.
    """
    import secrets as _secrets
    return _secrets.token_hex(32)


@pytest.fixture()
def store(db_session):
    """DbNonceStore bound to the test session factory.

    `db_session` reconfigures `database.SessionLocal` onto an in-memory SQLite
    engine with the schema created, so handing the factory in directly gives
    the store a real table with real primary-key semantics.
    """
    import app.database as database
    return DbNonceStore(database.SessionLocal)


class TestDbNonceStore:
    def test_first_use_is_admitted(self, store):
        assert store.set("a2a:nonce:n1", "1", nx=True, ex=600) is True

    def test_second_use_is_refused(self, store):
        assert store.set("a2a:nonce:n1", "1", nx=True, ex=600) is True
        assert store.set("a2a:nonce:n1", "1", nx=True, ex=600) is False

    def test_distinct_nonces_are_independent(self, store):
        assert store.set("a2a:nonce:n1", "1", nx=True, ex=600) is True
        assert store.set("a2a:nonce:n2", "1", nx=True, ex=600) is True

    def test_expired_record_is_reusable(self, store):
        """Past the TTL the timestamp-skew check has already rejected the
        request on its own, so refreshing here cannot admit anything."""
        from app.models import A2ANonce
        import app.database as database

        assert store.set("a2a:nonce:old", "1", nx=True, ex=600) is True
        db = database.SessionLocal()
        try:
            row = db.get(A2ANonce, "a2a:nonce:old")
            row.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
            db.commit()
        finally:
            db.close()
        assert store.set("a2a:nonce:old", "1", nx=True, ex=600) is True

    def test_only_setnx_is_implemented(self, store):
        """An unsupported form must raise rather than silently admit."""
        with pytest.raises(NotImplementedError):
            store.set("a2a:nonce:x", "1", nx=False, ex=600)


class TestVerifyRejectsReplay:
    """End to end through the vendored verifier, with the store attached."""

    def _signed(self, secret):
        return sign(_BODY, secret)

    def test_first_delivery_is_accepted(self, store, secret):
        ok, err = verify(self._signed(secret), _BODY, secret, redis_client=store)
        assert ok is True and err is None

    def test_identical_replay_is_rejected(self, store, secret):
        headers = self._signed(secret)
        ok, _ = verify(headers, _BODY, secret, redis_client=store)
        assert ok is True
        ok2, err2 = verify(headers, _BODY, secret, redis_client=store)
        assert ok2 is False
        assert err2 == "replay_detected"

    def test_replay_is_undetected_without_a_store(self, store, secret):
        """Pins the behaviour that made this a defect.

        With `redis_client=None` the check is SKIPPED, not failed — so the same
        bytes verify twice. This is the shape the middleware shipped with; the
        test exists so that reverting to None fails loudly here rather than
        silently removing the control.
        """
        headers = self._signed(secret)
        assert verify(headers, _BODY, secret, redis_client=None)[0] is True
        assert verify(headers, _BODY, secret, redis_client=None)[0] is True

    def test_a_fresh_signature_is_still_accepted(self, store, secret):
        """Replay protection must not break ordinary repeat traffic — each
        send has its own nonce."""
        for _ in range(5):
            ok, err = verify(self._signed(secret), _BODY, secret, redis_client=store)
            assert ok is True, err


class TestMiddlewareWiring:
    def test_middleware_builds_a_store_rather_than_passing_none(self):
        """The regression itself: the store must be attached at the call site."""
        from app.a2a_common.hmac_middleware import PartnerHmacMiddleware

        mw = PartnerHmacMiddleware(lambda *a: None)
        assert mw._nonce_store() is not None

    def test_store_is_built_once(self):
        from app.a2a_common.hmac_middleware import PartnerHmacMiddleware

        mw = PartnerHmacMiddleware(lambda *a: None)
        assert mw._nonce_store() is mw._nonce_store()
