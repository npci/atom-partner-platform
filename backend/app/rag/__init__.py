# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Partner-side RAG subsystem.

Standalone vector-retrieval foundation shared by the Document RAG (authority change
documents + a partner knowledge base) and the Code RAG (the partner's own GitLab
repo). Duplicated from the authority's `backend/app/rag/` stack rather than imported —
the partner platform is a separate deployable (mirrors the `a2a_common`
convention).
"""
