# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Code chunking for the Code RAG.

Line-aware, size-bounded chunks with a few lines of overlap so a retrieved chunk
keeps enough surrounding context (imports, the enclosing signature) to be useful.
Pragmatic — no tree-sitter / AST parsing (that's the NPCI heavy path); for
partner-side retrieval, bounded line windows over each file are enough and work
for any language.
"""
from __future__ import annotations

TARGET_CHARS = 1500
OVERLAP_LINES = 5
MIN_CHUNK_CHARS = 40


def chunk_code(content: str, *, target: int = TARGET_CHARS, overlap_lines: int = OVERLAP_LINES) -> list[str]:
    """Split source `content` into ~`target`-char chunks on line boundaries,
    carrying `overlap_lines` of trailing context into the next chunk."""
    content = content or ""
    if not content.strip():
        return []
    if len(content) <= target:
        return [content]

    lines = content.splitlines()
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in lines:
        # A single pathologically long line still gets emitted whole (don't
        # split mid-line — keeps tokens/identifiers intact).
        if size + len(line) + 1 > target and buf:
            chunks.append("\n".join(buf))
            buf = buf[-overlap_lines:] if overlap_lines else []
            size = sum(len(x) + 1 for x in buf)
        buf.append(line)
        size += len(line) + 1
    tail = "\n".join(buf)
    if tail.strip() and len(tail) >= MIN_CHUNK_CHARS:
        chunks.append(tail)
    elif tail.strip() and chunks:
        chunks[-1] = chunks[-1] + "\n" + tail
    elif tail.strip():
        chunks.append(tail)
    return chunks
