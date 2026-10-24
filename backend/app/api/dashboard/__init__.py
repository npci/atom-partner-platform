"""Partner Dashboard API — internal endpoints for the partner UI.

Split per domain (WS5) from the original 1399-line module. Each submodule owns
one domain's routes; this package re-exports a single aggregate `router` so
`from app.api.dashboard import router` and every `/api/...` URL are unchanged.
"""
from fastapi import APIRouter

from . import (
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
    npci_status,
    profile,
    progress,
    queries,
    settings,
    testing,
)

router = APIRouter()
for _module in (changes, queries, decision, defects, drafts, progress, certification, settings, design, testing, code, knowledge, code_repo, jobs, profile, npci_status):
    router.include_router(_module.router)

__all__ = ["router"]
