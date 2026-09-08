# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Database-backed nonce store for A2A replay protection.

WHY THIS EXISTS
`hmac_signer.verify()` takes a `redis_client` and, when it is None, SKIPS the
nonce check entirely — the comment there says most partner stacks have no
redis, which is true. `hmac_middleware` passed `redis_client=None`
unconditionally, so on this platform the only bound on replaying a captured,
correctly-signed request was the 300-second timestamp skew window, and
`"replay_detected"` was unreachable.

That window is not narrow enough for what the handlers do with a replayed
message. `cert_test_response` / `cert_completion_signoff` flip `cert_status`
to "certified"; `change_communication` re-inserts every ChangeDocument and can
reset a prior accept to pending; `cert_lifecycle` fans out a background
`fire_trigger` per case id, each carrying the stored `cert_trigger_secret` as
a bearer token; `cert_verdict_notification` opens a fix round and spawns a
full LLM run. Anyone able to capture one signed request — a TLS-terminating
proxy, log or PCAP access, a compromised hop — could replay it for five
minutes.

WHY THE DATABASE RATHER THAN MAKING REDIS MANDATORY
The reasoning that kept redis optional still holds: this is a reference
implementation partner organisations fork, and requiring new infrastructure to
close a defect would mean most forks never close it. But every fork already
has Postgres — it holds the settings this middleware reads on the same
request. A unique primary key gives exactly the SETNX semantics the check
needs, and the row is short-lived.

Redis stays preferred where present: it is faster and shared across replicas
without contention. This is what runs when it is absent, instead of nothing.

INTERFACE
Duck-types the one redis call `hmac_signer` makes:

    client.set(key, "1", nx=True, ex=<ttl>) -> truthy if newly set

so `hmac_signer.py` — which is vendored and hash-pinned (see VENDORED.md) —
needs no change.

FAIL-CLOSED
An exception here propagates to `hmac_signer.verify()`, which catches it,
logs the TYPE only, and returns `nonce_check_unavailable` → the request is
REJECTED. A database outage therefore costs availability on the A2A ingress
rather than silently costing replay protection, matching the house rule the
rest of that function already follows.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

# Probability of sweeping expired rows on any given call. The table is
# self-limiting at (request rate x TTL), so this only has to keep pace, not run
# every time — at 20 rps and a 600s TTL the steady state is ~12k rows.
_SWEEP_PROBABILITY = 0.01


class DbNonceStore:
    """SETNX-equivalent over a `a2a_nonces` row, keyed on the nonce."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    def set(self, key: str, value: str, *, nx: bool = True, ex: int = 600) -> bool:
        """Record `key` as seen. Returns False if it was already recorded.

        Only the `nx=True` form is implemented, because it is the only form
        the caller uses; anything else is a programming error rather than a
        case to handle silently.
        """
        if not nx:
            raise NotImplementedError(
                "DbNonceStore only implements the nx=True (SETNX) form")

        from app.models import A2ANonce

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        expires_at = now + timedelta(seconds=int(ex))

        db = self._session_factory()
        try:
            if random.random() < _SWEEP_PROBABILITY:
                db.execute(delete(A2ANonce).where(A2ANonce.expires_at < now))
                db.commit()

            db.add(A2ANonce(nonce=key, expires_at=expires_at))
            try:
                db.commit()
                return True
            except IntegrityError:
                # Primary-key collision — the nonce is already recorded. This
                # is the replay signal, and it is decided by the DATABASE's
                # uniqueness constraint rather than by a read-then-write in
                # application code, so two concurrent replays cannot both pass.
                db.rollback()

            existing = db.get(A2ANonce, key)
            if existing is None:
                # Raced with the sweep between the failed insert and this read.
                # The nonce is not currently recorded, so record it again.
                db.add(A2ANonce(nonce=key, expires_at=expires_at))
                try:
                    db.commit()
                    return True
                except IntegrityError:
                    db.rollback()
                    return False
            if existing.expires_at < now:
                # Recorded, but outside the replay window it was stored for.
                # Refresh rather than reject: past the TTL the timestamp-skew
                # check has already rejected the request on its own, so
                # treating it as fresh here cannot admit anything.
                existing.expires_at = expires_at
                db.commit()
                return True
            return False
        finally:
            db.close()


def build_nonce_store(redis_client=None):
    """The nonce store to hand `hmac_signer.verify()`.

    Prefers an injected redis client, then a configured redis URL, then the
    database. Never returns None — returning None is what disabled the check.
    """
    if redis_client is not None:
        return redis_client

    try:
        from app.config import settings
        redis_url = getattr(settings, "partner_rate_limit_redis_url", "") or ""
    except Exception:  # noqa: BLE001 — settings unavailable in some test contexts
        redis_url = ""

    if redis_url:
        try:
            import redis as _redis
            client = _redis.Redis.from_url(redis_url, socket_timeout=2)
            client.ping()
            logger.info("a2a nonce store: shared redis active")
            return client
        except Exception as exc:  # noqa: BLE001
            # Type only — redis-py builds messages from the connection URL,
            # which carries the password.
            logger.warning(
                "a2a nonce store: redis unavailable (%s); using the database",
                type(exc).__name__,
            )

    from app.database import SessionLocal
    return DbNonceStore(SessionLocal)
