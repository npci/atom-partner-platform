# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The Ollama embedding path must be guarded by the `ollama_embed` circuit
breaker (EA_Skills.md P7/P8).

The boundary's limits were declared in core/hostility.py from the start — H2,
threshold 8, 15s cooldown — but nothing enforced them. A down Ollama was
therefore retried on every batch AND every per-item fallback, each paying the
full 120s read timeout, so one indexing run could hang for hours.

Crucially, adding the breaker must NOT change the path's failure contract: this
is a fail-SOFT boundary. `_embed_batch` returns None (caller falls back) and
`_embed_one` returns a zero vector. A tripped breaker must degrade the same
way, never raise into the indexing run.
"""
import pytest

from app.core import resilience
from app.rag import embeddings


@pytest.fixture(autouse=True)
def _reset_breakers():
    resilience.reset_for_tests()
    yield
    resilience.reset_for_tests()


class TestBreakerIsWired:
    def test_embed_batch_failures_are_counted_by_the_breaker(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("ollama down")

        monkeypatch.setattr(embeddings.httpx, "post", boom)

        for _ in range(3):
            assert embeddings._embed_batch(["t"]) is None

        breaker = resilience.breaker_for("ollama_embed")
        assert breaker._consecutive_failures >= 3, "failures were not recorded by the breaker"

    def test_circuit_opens_and_then_short_circuits_without_calling(self, monkeypatch):
        calls = {"n": 0}

        def boom(*a, **kw):
            calls["n"] += 1
            raise RuntimeError("ollama down")

        monkeypatch.setattr(embeddings.httpx, "post", boom)

        threshold = resilience.breaker_for("ollama_embed").failure_threshold
        for _ in range(threshold):
            embeddings._embed_batch(["t"])
        assert resilience.breaker_for("ollama_embed").state == "open"

        calls_before = calls["n"]
        for _ in range(5):
            assert embeddings._embed_batch(["t"]) is None
        assert calls["n"] == calls_before, (
            "an open circuit still issued HTTP calls — the point is to stop "
            "paying the 120s timeout once Ollama is known to be down"
        )


class TestFailSoftContractPreserved:
    def test_open_circuit_returns_none_from_embed_batch_not_raise(self, monkeypatch):
        monkeypatch.setattr(
            embeddings.httpx, "post",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("down")),
        )
        threshold = resilience.breaker_for("ollama_embed").failure_threshold
        for _ in range(threshold):
            embeddings._embed_batch(["t"])

        assert embeddings._embed_batch(["t"]) is None  # must not raise

    def test_open_circuit_returns_zero_vector_from_embed_one_not_raise(self, monkeypatch):
        monkeypatch.setattr(
            embeddings.httpx, "post",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("down")),
        )
        threshold = resilience.breaker_for("ollama_embed").failure_threshold
        for _ in range(threshold):
            embeddings._embed_one("t")

        vec = embeddings._embed_one("t")
        assert embeddings.is_zero(vec), "must degrade to a zero vector, not raise"
        assert len(vec) == embeddings.settings.embed_dim

    def test_embed_texts_still_returns_aligned_output_when_ollama_is_down(self, monkeypatch):
        """The 1:1 alignment guarantee must survive a fully open circuit."""
        monkeypatch.setattr(
            embeddings.httpx, "post",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("down")),
        )
        texts = ["a", "b", "c"]
        out = embeddings.embed_texts(texts)
        assert len(out) == len(texts)
        assert all(embeddings.is_zero(v) for v in out)


class TestHappyPathUnaffected:
    def test_successful_batch_returns_embeddings(self, monkeypatch):
        class _Resp:
            def raise_for_status(self): pass
            def json(self): return {"embeddings": [[0.1] * 768, [0.2] * 768]}

        monkeypatch.setattr(embeddings.httpx, "post", lambda *a, **kw: _Resp())
        out = embeddings._embed_batch(["a", "b"])
        assert out is not None and len(out) == 2

    def test_length_mismatch_guard_still_fires(self, monkeypatch):
        """The pre-existing H2 guard must not be lost to the refactor."""
        class _Resp:
            def raise_for_status(self): pass
            def json(self): return {"embeddings": [[0.1] * 768]}  # 1 for 2 inputs

        monkeypatch.setattr(embeddings.httpx, "post", lambda *a, **kw: _Resp())
        assert embeddings._embed_batch(["a", "b"]) is None
