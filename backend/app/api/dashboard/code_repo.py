# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Dashboard domain: code-repo — register the partner's GitLab repository for
the Code RAG (Phase 3.1) and trigger indexing.

The repo source is fetched over the GitLab API, chunked + embedded into
`document_chunks` (doc_category='code'); the design/code/test agents retrieve it
(Phase 3.2). The GitLab token is stored write-only in `partner_settings` (key
'gitlab_token') and never returned.
"""
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.core.secret_box import encrypt
from app.database import SessionLocal, get_db
from app.models import CodeRepo, PartnerSetting, PartnerUser
from app.rag.code_ingestion import ingest_repo
from app.rag.doc_ingest import _delete_chunks

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["dashboard"])


class CodeRepoCreate(BaseModel):
    label: str
    gitlab_repo: str           # "group/project"
    gitlab_branch: str = "main"
    gitlab_url: str | None = None
    languages: str | None = None      # csv, e.g. "java,python"; null = all known
    gitlab_token: str | None = None   # optional — stored write-only if supplied


class TokenBody(BaseModel):
    token: str


def _repo_view(r: CodeRepo) -> dict:
    return {
        "id": r.id, "label": r.label, "gitlab_repo": r.gitlab_repo,
        "gitlab_branch": r.gitlab_branch, "gitlab_url": r.gitlab_url,
        "languages": r.languages, "status": r.status, "last_error": r.last_error,
        "last_sha": r.last_sha, "files_count": r.files_count,
        "chunks_count": r.chunks_count,
        "last_indexed_at": r.last_indexed_at.isoformat() if r.last_indexed_at else None,
    }


def _set_gitlab_token(db: Session, token: str) -> None:
    # Encrypted at rest (docs/adr/ADR-0002-secrets-vault-migration.md) — same
    # AES-256-GCM envelope as the Settings-UI secret fields.
    encrypted = encrypt(token)
    row = db.get(PartnerSetting, "gitlab_token")
    if row:
        row.value = encrypted
    else:
        db.add(PartnerSetting(key="gitlab_token", value=encrypted))
    db.commit()


@router.put("/code-repo/token")
def set_gitlab_token(
    body: TokenBody,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Set the GitLab access token (write-only — stored in partner_settings)."""
    if not body.token.strip():
        raise HTTPException(status_code=400, detail="token is required")
    _set_gitlab_token(db, body.token.strip())
    return {"ok": True}


@router.get("/code-repo")
def list_code_repos(
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List registered repos + index status. Also reports whether a token is set."""
    rows = db.execute(select(CodeRepo).order_by(CodeRepo.created_at.desc())).scalars().all()
    token_set = db.get(PartnerSetting, "gitlab_token") is not None
    return {"token_set": token_set, "repos": [_repo_view(r) for r in rows]}


@router.post("/code-repo")
def create_code_repo(
    body: CodeRepoCreate,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Register a repo for the Code RAG. If `gitlab_token` is supplied it is
    stored write-only. Does not index — call POST /code-repo/{id}/index."""
    if not body.label.strip() or not body.gitlab_repo.strip():
        raise HTTPException(status_code=400, detail="label and gitlab_repo are required")
    if body.gitlab_token:
        _set_gitlab_token(db, body.gitlab_token.strip())
    repo = CodeRepo(
        label=body.label.strip(), gitlab_repo=body.gitlab_repo.strip(),
        gitlab_branch=(body.gitlab_branch or "main").strip(),
        gitlab_url=(body.gitlab_url or None), languages=(body.languages or None),
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    return _repo_view(repo)


@router.delete("/code-repo/{repo_id}")
def delete_code_repo(
    repo_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a repo + its indexed code chunks."""
    repo = db.get(CodeRepo, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="code repo not found")
    _delete_chunks(db, doc_category="code", repo_id=repo_id)
    db.delete(repo)
    db.commit()
    return {"deleted": True}


def _index_job(repo_id: str) -> None:
    """Background index — owns its own DB session (the request's is closed by the
    time this runs)."""
    db = SessionLocal()
    try:
        ingest_repo(db, repo_id)
    except Exception:  # noqa: BLE001 — already logged + status stamped in ingest_repo
        logger.warning("background code index failed for repo=%s", repo_id)
    finally:
        db.close()


@router.post("/code-repo/{repo_id}/index", status_code=202)
def index_code_repo(
    repo_id: str,
    bg: BackgroundTasks,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Kick a background full re-index. Returns immediately; poll GET /code-repo
    for status (idle → indexing → indexed/error)."""
    repo = db.get(CodeRepo, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="code repo not found")
    if db.get(PartnerSetting, "gitlab_token") is None:
        raise HTTPException(status_code=400, detail="set a GitLab token first (PUT /code-repo/token)")
    if repo.status == "indexing":
        raise HTTPException(status_code=409, detail="already indexing")
    bg.add_task(_index_job, repo_id)
    return {"status": "indexing", "repo_id": repo_id}
