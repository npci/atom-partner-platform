# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Partner Dashboard API — internal endpoints for the partner UI.

Split per domain (WS5) from the original 1399-line module. Each submodule owns
one domain's routes; this package re-exports a single aggregate `router` so
`from app.api.dashboard import router` and every `/api/...` URL are unchanged.
"""
from fastapi import APIRouter

from . import (
    cert_fix,
    certification,
    changes,
    code,
    code_repo,
    decision,
    defects,
    design,
    drafts,
    jobs,
    knowledge,
    profile,
    progress,
    queries,
    settings,
    testing,
)

router = APIRouter()
for _module in (changes, queries, decision, defects, drafts, progress, certification, cert_fix, settings, design, testing, code, knowledge, code_repo, jobs, profile):
    router.include_router(_module.router)

__all__ = ["router"]
