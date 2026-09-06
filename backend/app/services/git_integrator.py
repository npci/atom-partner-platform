# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Git Integrator (partner side) — Code RAG Phase 3.4.

Pushes generated file contents to the partner's OWN GitLab repo as a merge
request for human review: create branch → commit files → open MR. It NEVER
merges — the partner's engineers review and merge themselves.

Standalone + deliberately simpler than the NPCI `backend/app/services/
git_integrator.py` (no multi-repo grouping, no Phase-B state machine, no
auto-merge). Reuses the repo handle + write-only token plumbing from
`rag/code_ingestion.py` so token storage stays in one place.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models import CodeRepo
from app.rag.code_ingestion import _gitlab_project, _gitlab_token

logger = logging.getLogger(__name__)


def _public_url(repo: CodeRepo, project) -> str:
    """Browser URL base for the MR link (localhost, not host.docker.internal)."""
    base = (repo.gitlab_url or "").replace("host.docker.internal", "localhost").rstrip("/")
    if not base:
        # python-gitlab exposes the project's web_url; fall back to it.
        base = (getattr(project, "web_url", "") or "").rsplit("/", maxsplit=project.path_with_namespace.count("/") + 1)[0]
    return base.rstrip("/")


def fetch_current_files(db: Session, repo: CodeRepo, paths: list[str]) -> dict[str, str]:
    """Fetch the current contents of `paths` from the repo's default branch so the
    whole-file generation pass can return full modified files. Missing files (new
    additions) are simply absent from the result. Fail-soft per file."""
    token = _gitlab_token(db)
    if not token or not paths:
        return {}
    branch = repo.gitlab_branch or "main"
    project = _gitlab_project(repo, token)
    out: dict[str, str] = {}
    for path in paths:
        try:
            raw = project.files.get(file_path=path, ref=branch)
            out[path] = raw.decode().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — absent = treat as a new file
            continue
    return out


def open_merge_request(
    db: Session,
    repo: CodeRepo,
    *,
    branch_name: str,
    files: list[dict],
    title: str,
    description: str,
) -> dict:
    """Create `branch_name` off the repo's default branch, commit `files`
    ([{path, content}]), and open an MR. Reuses an existing branch/MR if present.
    Never merges. Returns {branch, commit_sha, mr_url, mr_iid, files_count}.

    Raises RuntimeError on misconfiguration (no token, no files); GitLab errors
    propagate so the caller can record status='error'."""
    if not files:
        raise RuntimeError("no files to commit")
    token = _gitlab_token(db)
    if not token:
        raise RuntimeError("no GitLab token configured (set one in Settings)")

    base_branch = repo.gitlab_branch or "main"
    project = _gitlab_project(repo, token)

    # Branch (reuse if a prior attempt created it).
    try:
        project.branches.create({"branch": branch_name, "ref": base_branch})
        logger.info("git_integrator: created branch %s off %s", branch_name, base_branch)
    except Exception as e:  # noqa: BLE001
        if "already exists" in str(e).lower():
            logger.info("git_integrator: branch %s exists, reusing", branch_name)
        else:
            raise

    # Commit — create vs update decided per file by existence on the branch.
    actions = []
    for f in files:
        try:
            project.files.get(file_path=f["path"], ref=branch_name)
            action = "update"
        except Exception:  # noqa: BLE001
            action = "create"
        actions.append({"action": action, "file_path": f["path"], "content": f["content"]})

    commit = project.commits.create({
        "branch": branch_name,
        "commit_message": title,
        "actions": actions,
    })
    commit_sha = commit.id
    logger.info("git_integrator: committed %d files, sha=%s", len(files), commit_sha[:12])

    # MR (reuse an open one for the same source branch).
    existing = project.mergerequests.list(
        source_branch=branch_name, target_branch=base_branch, state="opened",
    )
    if existing:
        mr = existing[0]
    else:
        mr = project.mergerequests.create({
            "source_branch": branch_name,
            "target_branch": base_branch,
            "title": title,
            "description": description,
            "remove_source_branch": True,
        })
    base = _public_url(repo, project)
    mr_url = f"{base}/{project.path_with_namespace}/-/merge_requests/{mr.iid}" if base \
        else getattr(mr, "web_url", None)
    logger.info("git_integrator: MR !%s ready: %s", mr.iid, mr_url)

    return {
        "branch": branch_name,
        "commit_sha": commit_sha,
        # Kept for logs/operator use; not serialised to the browser (see _mr_view).
        "mr_url": mr_url,
        # The components the UI needs to rebuild the link itself, so no URL
        # string has to cross the API boundary into an href.
        "mr_iid": mr.iid,
        "project_path": project.path_with_namespace,
        "files_count": len(files),
    }
