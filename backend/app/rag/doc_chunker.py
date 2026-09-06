# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Markdown/text chunking for the Document RAG.

Size-bounded, paragraph-aware chunks with a small overlap so a retrieved chunk
carries enough surrounding context to be useful on its own. Deliberately simple
(no token counting / tree-sitter) — change documents and KB docs are prose +
tables, not code; the code chunker (Phase 3.1) is a separate concern.
"""
from __future__ import annotations

# ~1500 chars ≈ 350-400 tokens — a few of these fit comfortably in a prompt
# alongside the full current-change docs (retrieval augments, not replaces).
TARGET_CHARS = 1500
OVERLAP_CHARS = 200
MIN_CHUNK_CHARS = 80


def chunk_text(text: str, *, target: int = TARGET_CHARS, overlap: int = OVERLAP_CHARS) -> list[str]:
    """Split `text` into overlapping chunks of ~`target` chars, breaking on
    paragraph (blank-line) boundaries where possible. Returns [] for empty text."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= target:
        return [text]

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        # A single oversized paragraph is hard-split below.
        if len(para) > target:
            if buf:
                chunks.append(buf)
                buf = ""
            for i in range(0, len(para), target - overlap):
                piece = para[i:i + target]
                if len(piece) >= MIN_CHUNK_CHARS:
                    chunks.append(piece)
            continue
        if buf and len(buf) + 2 + len(para) > target:
            chunks.append(buf)
            # Carry an overlap tail from the chunk just emitted.
            tail = buf[-overlap:] if overlap else ""
            buf = (tail + "\n\n" + para).strip()
        else:
            buf = (buf + "\n\n" + para).strip() if buf else para
    if buf and len(buf) >= MIN_CHUNK_CHARS:
        chunks.append(buf)
    elif buf and chunks:
        chunks[-1] = (chunks[-1] + "\n\n" + buf).strip()
    elif buf:
        chunks.append(buf)
    return chunks
