# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the per-change context cache in agents/revision_context.py
(Finding 8: security_architecture_skills.md §5.5, EA_Skills.md P6/P2)."""
import pytest

from app.agents import revision_context
from app.models import ChangeDocument


@pytest.fixture(autouse=True)
def _clear_cache():
    revision_context.invalidate_context_cache()
    yield
    revision_context.invalidate_context_cache()


def _add_doc(db_session, change_id, doc_type, content, version=1):
    db_session.add(ChangeDocument(
        change_id=change_id, doc_type=doc_type, content=content, negotiation_version=version,
    ))
    db_session.commit()


class TestCacheHitAvoidsRefetch:
    def test_second_call_returns_identical_result_without_new_query(self, db_session, monkeypatch):
        _add_doc(db_session, "c1", "brd", "hello world")

        calls = {"n": 0}
        real_uncached = revision_context._assemble_change_context_uncached

        def _counting_uncached(db, change_id, api_key=None):
            calls["n"] += 1
            return real_uncached(db, change_id, api_key)

        monkeypatch.setattr(revision_context, "_assemble_change_context_uncached", _counting_uncached)

        first = revision_context.assemble_change_context(db_session, "c1")
        second = revision_context.assemble_change_context(db_session, "c1")

        assert first == second
        assert calls["n"] == 1  # only the FIRST call did real work

    def test_different_changes_do_not_share_cache_entries(self, db_session):
        _add_doc(db_session, "c1", "brd", "content one")
        _add_doc(db_session, "c2", "brd", "content two")

        r1 = revision_context.assemble_change_context(db_session, "c1")
        r2 = revision_context.assemble_change_context(db_session, "c2")

        assert r1["documents"][0]["content"] == "content one"
        assert r2["documents"][0]["content"] == "content two"


class TestFingerprintInvalidation:
    def test_new_document_row_invalidates_cache(self, db_session):
        _add_doc(db_session, "c1", "brd", "v1 content")
        first = revision_context.assemble_change_context(db_session, "c1")
        assert len(first["documents"]) == 1

        _add_doc(db_session, "c1", "tech_spec", "a second doc type")
        second = revision_context.assemble_change_context(db_session, "c1")
        assert len(second["documents"]) == 2  # picked up immediately, no TTL wait

    def test_negotiation_version_bump_invalidates_cache(self, db_session, monkeypatch):
        _add_doc(db_session, "c1", "brd", "v1 content", version=1)
        first = revision_context.assemble_change_context(db_session, "c1")
        assert first["current_version"] == 1

        # Avoid a real LLM call for the revision summary — degrade path is fine.
        monkeypatch.setattr(
            revision_context, "_summarize_changes",
            lambda changed, current_version, api_key: "summary",
        )
        _add_doc(db_session, "c1", "brd", "v2 content", version=2)
        second = revision_context.assemble_change_context(db_session, "c1")
        assert second["current_version"] == 2  # picked up immediately, no TTL wait


class TestInvalidateContextCache:
    def test_explicit_invalidation_forces_refetch(self, db_session, monkeypatch):
        _add_doc(db_session, "c1", "brd", "hello world")

        calls = {"n": 0}
        real_uncached = revision_context._assemble_change_context_uncached

        def _counting_uncached(db, change_id, api_key=None):
            calls["n"] += 1
            return real_uncached(db, change_id, api_key)

        monkeypatch.setattr(revision_context, "_assemble_change_context_uncached", _counting_uncached)

        revision_context.assemble_change_context(db_session, "c1")
        revision_context.invalidate_context_cache("c1")
        revision_context.assemble_change_context(db_session, "c1")

        assert calls["n"] == 2

    def test_invalidate_all_clears_every_change(self, db_session):
        _add_doc(db_session, "c1", "brd", "one")
        _add_doc(db_session, "c2", "brd", "two")
        revision_context.assemble_change_context(db_session, "c1")
        revision_context.assemble_change_context(db_session, "c2")
        assert set(revision_context._CONTEXT_CACHE.keys()) == {"c1", "c2"}

        revision_context.invalidate_context_cache()
        assert revision_context._CONTEXT_CACHE == {}


class TestEmptyChange:
    def test_no_documents_returns_empty_shape_and_is_cached(self, db_session):
        result = revision_context.assemble_change_context(db_session, "no-such-change")
        assert result == {"documents": [], "revision_summary": None, "current_version": 1}
        # Cached too — a second call must not error and must return the same shape.
        again = revision_context.assemble_change_context(db_session, "no-such-change")
        assert again == result
