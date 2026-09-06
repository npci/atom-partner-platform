# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Code RAG ingestion — fetch a partner GitLab repo over the API, chunk + embed
its source, and store into the shared `document_chunks` pgvector table
(doc_category='code', repo_id=<CodeRepo.id>).

Standalone copy of the NPCI ingest pipeline, adapted to the partner's Ollama
embeddings + the shared chunk store. Deliberately simpler than NPCI's
(no symbol graph / LSP / multi-pass): a FULL re-index each run — delete the
repo's code chunks, re-fetch + re-embed. The embed cache (rag/embed_cache.py)
makes unchanged files free, and a full listing sidesteps the partial-set
deletion bug (uat finding C1) entirely — deletions can't be inferred wrongly
when every chunk for the repo is replaced.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.models import CodeRepo, PartnerSetting
from app.rag.code_chunker import chunk_code
from app.rag.doc_ingest import _delete_chunks, _store_chunks

logger = logging.getLogger(__name__)

LANGUAGE_EXTENSIONS: dict[str, str] = {
    ".java": "java", ".py": "python",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript",
    ".go": "go", ".kt": "kotlin", ".rb": "ruby", ".cs": "csharp",
}

# Skip vendored / generated / binary-ish trees so we don't burn embeddings on noise.
_SKIP_DIRS = ("node_modules/", "/dist/", "/build/", "/target/", "/.git/", "vendor/", "/__pycache__/")
_MAX_FILE_CHARS = 200_000  # skip pathologically large files


def _detect_language(path: str) -> str | None:
    lowered = (path or "").lower()
    for ext, lang in LANGUAGE_EXTENSIONS.items():
        if lowered.endswith(ext):
            return lang
    return None


def _gitlab_token(db: Session) -> str | None:
    """Decrypts transparently if the stored value is in core.secret_box's
    enc:v1: form (docs/adr/ADR-0002-secrets-vault-migration.md); returns
    legacy plaintext unchanged for rows written before that module existed."""
    row = db.get(PartnerSetting, "gitlab_token")
    if not row or not row.value:
        return None
    from app.core.secret_box import decrypt
    try:
        return decrypt(row.value)
    except Exception:  # noqa: BLE001 — corrupted/tamper-evident; surface as absent
        logger.critical("_gitlab_token: failed to decrypt — treating as unconfigured")
        return None


def _gitlab_project(repo: CodeRepo, token: str):
    """Build a python-gitlab project handle for `repo`. Raises with a clear
    message if the SDK isn't installed or auth fails.

    This is the single construction point for every GitLab call in the
    platform — repo indexing, symbol search, file fetch, and the merge-request
    path all obtain their handle here — so it is where the `gitlab_api`
    boundary's limits are applied (EA_Skills.md P7/P8;
    docs/adr/ADR-0004-hostility-tier-registry.md).

    Two controls, and it matters that they cover different things:

      * **`timeout=`** is passed to the client, and python-gitlab hands it to
        the underlying `requests` session — so it bounds not just this
        `projects.get()` but EVERY later call made through the returned
        handle (`repository_tree`, `files.get`, `branches.create`, ...). Those
        calls happen after this function returns and cannot be wrapped from
        here, which makes the client-level timeout the only control that
        reaches them. python-gitlab's default is `None`, i.e. **no timeout at
        all**: a hung GitLab could block a worker indefinitely.

      * **Breaker + bulkhead** wrap the handshake below. `projects.get()` is a
        real authenticated round-trip, so it is a reliable canary for "GitLab
        is unreachable / the token is rejected" — once the circuit opens, an
        indexing run stops paying the full timeout per call and fails fast.
    """
    try:
        import gitlab as gl_module
    except ImportError as e:
        raise RuntimeError("python-gitlab not installed — add it to requirements.txt") from e

    from app.core.hostility import get as get_boundary
    from app.core.resilience import breaker_for, bulkhead_for

    limits = get_boundary("gitlab_api")
    base = (repo.gitlab_url or settings.partner_gitlab_url or "").rstrip("/")
    if "://localhost" in base:
        base = base.replace("://localhost", "://host.docker.internal")

    glab = gl_module.Gitlab(
        base,
        private_token=token,
        keep_base_url=True,
        # (connect, read) — requests' 2-tuple form, so a stalled connect and a
        # stalled response are bounded independently.
        timeout=(limits.timeout_connect_s, limits.timeout_read_s),
    )

    # Deliberately NOT softened: every caller of this function treats a failure
    # as fatal (indexing marks the repo errored, the MR path raises). Swallowing
    # an open circuit here would hand back a handle that fails later, further
    # from the cause.
    with bulkhead_for("gitlab_api").acquire(timeout=10.0):
        with breaker_for("gitlab_api").call():
            return glab.projects.get(repo.gitlab_repo)


def _wanted_extensions(repo: CodeRepo) -> set[str]:
    if not repo.languages:
        return set(LANGUAGE_EXTENSIONS)
    wanted = {l.strip().lower() for l in repo.languages.split(",") if l.strip()}
    return {ext for ext, lang in LANGUAGE_EXTENSIONS.items() if lang in wanted} or set(LANGUAGE_EXTENSIONS)


def ingest_repo(db: Session, repo_id: str) -> dict:
    """Full re-index of a registered repo. Updates the CodeRepo row's status +
    counts + last_sha. Returns {files, chunks}. Designed to run in a background
    task (fetch + embed is slow). Raises on hard failure after marking status."""
    repo = db.get(CodeRepo, repo_id)
    if repo is None:
        raise RuntimeError(f"unknown code repo: {repo_id}")

    token = _gitlab_token(db)
    if not token:
        repo.status = "error"
        repo.last_error = "no GitLab token configured (partner_settings 'gitlab_token')"
        db.commit()
        raise RuntimeError(repo.last_error)

    repo.status = "indexing"
    repo.last_error = None
    db.commit()

    try:
        project = _gitlab_project(repo, token)
        branch = repo.gitlab_branch or "main"
        # Branch head SHA — for display / future incremental.
        try:
            head_sha = project.branches.get(branch).attributes.get("commit", {}).get("id")
        except Exception:  # noqa: BLE001
            head_sha = None

        exts = _wanted_extensions(repo)
        tree = project.repository_tree(ref=branch, recursive=True, all=True)
        files = [
            it for it in tree
            if it.get("type") == "blob"
            and any(it["path"].lower().endswith(e) for e in exts)
            and not any(s in ("/" + it["path"].lower()) for s in _SKIP_DIRS)
        ]
        cap = settings.code_index_max_files
        if cap and len(files) > cap:
            logger.warning("code index: repo=%s has %d files, capping at %d", repo.gitlab_repo, len(files), cap)
            files = files[:cap]

        # Full re-index: drop the repo's existing code chunks, then re-ingest.
        _delete_chunks(db, doc_category="code", repo_id=repo.id)

        total_chunks = 0
        indexed_files = 0
        for it in files:
            path = it["path"]
            try:
                raw = project.files.get(file_path=path, ref=branch)
                content = raw.decode().decode("utf-8", errors="replace")
            except Exception as exc:  # noqa: BLE001
                logger.warning("code index: could not fetch %s", path, exc_info=True)
                continue
            if len(content) > _MAX_FILE_CHARS or not content.strip():
                continue
            chunks = chunk_code(content)
            if not chunks:
                continue
            total_chunks += _store_chunks(
                db,
                doc_category="code",
                source_key=path,
                chunks=chunks,
                repo_id=repo.id,
                base_metadata={"path": path, "language": _detect_language(path)},
            )
            indexed_files += 1
            # Commit per-file so a long index is incremental + visible, and a
            # mid-run failure leaves a partial-but-valid index rather than rolling
            # everything back.
            db.commit()

        repo.status = "indexed"
        repo.files_count = indexed_files
        repo.chunks_count = total_chunks
        repo.last_sha = head_sha
        repo.last_indexed_at = datetime.now(timezone.utc)
        db.commit()
        logger.info("code index done: repo=%s files=%d chunks=%d sha=%s",
                    repo.gitlab_repo, indexed_files, total_chunks, (head_sha or "")[:8])
        return {"files": indexed_files, "chunks": total_chunks}
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        repo = db.get(CodeRepo, repo_id)
        if repo:
            from app.core.errors import user_facing_error
            repo.status = "error"
            # Surfaced in the repo list; sanitised for the same reason as
            # AgentJob.error. The `logger.exception` below keeps everything.
            repo.last_error = user_facing_error(exc)
            db.commit()
        logger.exception("code index failed: repo=%s", repo_id)
        raise
