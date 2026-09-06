# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The revision-context cache must be BOUNDED with LRU eviction
(EA_Skills.md P2 — "Flag when: unbounded queues are used")."""
import pytest

from app.agents import revision_context
from app.config import settings


@pytest.fixture(autouse=True)
def _clear_cache():
    revision_context.invalidate_context_cache()
    yield
    revision_context.invalidate_context_cache()


def _fill(n: int, monkeypatch, start: int = 0):
    """Insert n entries via the public path, stubbing out the DB fingerprint
    and the expensive uncached assembly."""
    monkeypatch.setattr(revision_context, "_fingerprint", lambda db, cid: (1, 1))
    monkeypatch.setattr(
        revision_context, "_assemble_change_context_uncached",
        lambda db, cid, key: {"change": cid},
    )
    for i in range(start, start + n):
        revision_context.assemble_change_context(None, f"change-{i}")


class TestBoundedness:
    def test_cache_never_exceeds_max_entries(self, monkeypatch):
        monkeypatch.setattr(settings, "context_cache_max_entries", 5)
        _fill(50, monkeypatch)
        stats = revision_context.context_cache_stats()
        assert stats["entries"] == 5, "cache grew past its cap — this is the P2 leak"
        assert stats["max_entries"] == 5

    def test_zero_cap_is_floored_to_one_not_disabled_silently(self, monkeypatch):
        monkeypatch.setattr(settings, "context_cache_max_entries", 0)
        _fill(3, monkeypatch)
        assert revision_context.context_cache_stats()["entries"] == 1

    def test_lowering_the_cap_converges_immediately(self, monkeypatch):
        """A `while` eviction loop (not a single pop) means a reduced cap takes
        effect on the very next insert rather than leaking one entry per call."""
        monkeypatch.setattr(settings, "context_cache_max_entries", 20)
        _fill(20, monkeypatch)
        assert revision_context.context_cache_stats()["entries"] == 20

        monkeypatch.setattr(settings, "context_cache_max_entries", 3)
        _fill(1, monkeypatch, start=100)
        assert revision_context.context_cache_stats()["entries"] == 3


class TestLruOrdering:
    def test_evicts_least_recently_used_not_oldest_inserted(self, monkeypatch):
        monkeypatch.setattr(settings, "context_cache_max_entries", 3)
        monkeypatch.setattr(revision_context, "_fingerprint", lambda db, cid: (1, 1))
        monkeypatch.setattr(
            revision_context, "_assemble_change_context_uncached",
            lambda db, cid, key: {"change": cid},
        )

        for cid in ("a", "b", "c"):
            revision_context.assemble_change_context(None, cid)

        # Touch "a" — it is now the most recently USED despite being the
        # oldest inserted, so inserting "d" must evict "b", not "a".
        revision_context.assemble_change_context(None, "a")
        revision_context.assemble_change_context(None, "d")

        keys = set(revision_context._CONTEXT_CACHE.keys())
        assert "a" in keys, "LRU touched entry was wrongly evicted (insertion-order eviction)"
        assert "b" not in keys
        assert keys == {"a", "c", "d"}


class TestStats:
    def test_stats_reports_size_and_capacity(self, monkeypatch):
        monkeypatch.setattr(settings, "context_cache_max_entries", 10)
        _fill(4, monkeypatch)
        assert revision_context.context_cache_stats() == {"entries": 4, "max_entries": 10}
