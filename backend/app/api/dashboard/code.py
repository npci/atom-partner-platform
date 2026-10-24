"""Dashboard domain: code — run the partner Code agent against a change to
author an implementation plan + change skeleton, persist a versioned plan, serve
it (JSON + .docx download).

When a repo is indexed (Code RAG), the plan is repository-grounded — retrieved
source excerpts are passed to the agent and _meta.grounded=True. With no indexed
repo it falls back to the spec-grounded skeleton (_meta.grounded=False). MR push
is the later Phase 3.4.
"""
import json
import logging
from urllib.parse import quote as urlquote

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import registry
from app.api.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.models import (
    CodeMergeRequest, CodeRepo, CodeReport, CodeReviewReport, DesignReport,
    GeneratedCodeFile, IncomingChange, PartnerSetting, PartnerUser,
)
from app.rag.retrieval import build_code_context, build_kb_context

from ._shared import markdown_to_docx_bytes

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["dashboard"])


def _change_query(change: IncomingChange) -> str:
    return f"{change.title or ''} {(change.enhanced_prompt or change.initial_prompt or '')[:500]}".strip()


def _kb_context(db: Session, change: IncomingChange) -> str:
    return build_kb_context(db, _change_query(change))


def _latest_indexed_repo(db: Session) -> CodeRepo | None:
    """The repo the code agent grounds against / pushes to: most-recently indexed."""
    return db.execute(
        select(CodeRepo)
        .where(CodeRepo.status == "indexed")
        .order_by(CodeRepo.last_indexed_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _code_context(db: Session, change: IncomingChange) -> str:
    """Retrieve source excerpts from the most-recently-indexed repo, scoped to it.
    Empty string when no repo is indexed → the agent falls back to spec-grounded."""
    repo = _latest_indexed_repo(db)
    if repo is None:
        return ""
    return build_code_context(db, _change_query(change), repo_id=repo.id)


def _runtime_api_key(db: Session) -> str | None:
    row = db.get(PartnerSetting, "partner_anthropic_api_key")
    return row.value if row and row.value else None


def _latest_design_summary(db: Session, change_id: str) -> str | None:
    row = db.execute(
        select(DesignReport)
        .where(DesignReport.change_id == change_id)
        .order_by(DesignReport.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if not row:
        return None
    try:
        return (json.loads(row.content) or {}).get("one_line_summary")
    except (json.JSONDecodeError, TypeError):
        return None


def _run_code_plan_job(change_id: str, db: Session, progress) -> None:
    """The actual code-plan run — executes inside a background AgentJob."""
    change = db.execute(
        select(IncomingChange).where(IncomingChange.id == change_id)
    ).scalar_one_or_none()
    if change is None:
        raise RuntimeError(f"unknown change_id: {change_id}")

    api_key = _runtime_api_key(db)

    progress("assembling change context")
    from app.agents.revision_context import assemble_change_context
    ctx = assemble_change_context(db, change_id, api_key)

    progress("retrieving repository context")
    agent_input = {
        "change_title": change.title,
        "change_initial_prompt": change.initial_prompt,
        "change_enhanced_prompt": change.enhanced_prompt,
        "documents": ctx["documents"],
        "revision_summary": ctx["revision_summary"],
        "design_summary": _latest_design_summary(db, change_id),
        "knowledge_context": _kb_context(db, change),
        "code_context": _code_context(db, change),
        "api_key": api_key,
        "change_id": change_id,
    }
    progress("generating implementation plan (takes a few minutes)")
    try:
        report = registry.get("code").execute(agent_input, db=db, change_id=change_id)
    except Exception as exc:  # noqa: BLE001 — surfaced on the job row + audited in agent_runs
        raise RuntimeError(
            "code agent failed — check logs. Common causes: PARTNER.md not "
            "mounted, no change documents, missing LLM provider key, or model "
            f"returned non-JSON. ({exc})"
        )

    prior = db.execute(
        select(CodeReport.version).where(CodeReport.change_id == change_id)
    ).scalars().all()
    next_version = (max(prior) + 1) if prior else 1

    meta = report.get("_meta", {})
    row = CodeReport(
        change_id=change_id,
        version=next_version,
        content=json.dumps(report),
        profile_version=meta.get("profile_version"),
        model_used=meta.get("model_used"),
    )
    db.add(row)
    db.commit()


@router.post("/changes/{change_id}/code/analyse", status_code=202)
def run_code_plan(
    change_id: str,
    bg: BackgroundTasks,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Kick a background Code-agent run; returns 202 + a job id immediately.
    Poll GET /changes/{id}/jobs/code/latest; the plan lands in code_reports."""
    change = db.execute(
        select(IncomingChange).where(IncomingChange.id == change_id)
    ).scalar_one_or_none()
    if change is None:
        raise HTTPException(status_code=404, detail=f"unknown change_id: {change_id}")
    from .jobs import start_job
    return start_job(
        db, bg, change_id=change_id, kind="code",
        runner=lambda jdb, progress: _run_code_plan_job(change_id, jdb, progress),
    )


def _latest_code_row(db: Session, change_id: str) -> CodeReport | None:
    return db.execute(
        select(CodeReport)
        .where(CodeReport.change_id == change_id)
        .order_by(CodeReport.version.desc())
        .limit(1)
    ).scalar_one_or_none()


@router.get("/changes/{change_id}/code/report")
def get_code_report(
    change_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the highest-version persisted code plan. 404 → none yet."""
    row = _latest_code_row(db, change_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no code plan yet")
    try:
        report = json.loads(row.content)
    except json.JSONDecodeError:
        logger.error("code report %s has invalid JSON content", row.id)
        raise HTTPException(status_code=500, detail="stored report is unreadable")
    return {
        "change_id": change_id,
        "version": row.version,
        "generated_at": row.generated_at.isoformat(),
        "profile_version": row.profile_version,
        "model_used": row.model_used,
        "report": report,
    }


@router.get("/changes/{change_id}/code/plan.docx")
def download_code_docx(
    change_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Render the latest code plan's markdown body as a .docx download."""
    row = _latest_code_row(db, change_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no code plan yet")
    try:
        report = json.loads(row.content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="stored report is unreadable")

    body_md = (report.get("plan_markdown") or "").strip()
    if not body_md:
        raise HTTPException(status_code=404, detail="code plan has no document body")

    title = f"Implementation plan — {report.get('one_line_summary', '')[:80]}"
    docx = markdown_to_docx_bytes(body_md, title=title)
    fname = f"implementation_plan_{change_id}_v{row.version}.docx"
    return Response(
        content=docx,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{urlquote(fname)}"'},
    )


# ── Merge request (Code RAG Phase 3.4) ──────────────────────────────────────


def _mr_view(row: CodeMergeRequest) -> dict:
    return {
        "id": row.id, "change_id": row.change_id, "repo_id": row.repo_id,
        "code_report_version": row.code_report_version, "branch": row.branch,
        "mr_url": row.mr_url, "mr_iid": row.mr_iid, "commit_sha": row.commit_sha,
        "files_count": row.files_count, "status": row.status, "last_error": row.last_error,
        "created_at": row.created_at.isoformat(),
    }


# ── Code generation (split from the MR push) ────────────────────────────────
# Files are generated once, persisted as a reviewable artifact (GeneratedCodeFile),
# then pushed by the MR step. This split is what lets the code-review / security-
# review loop run over the files before they ever reach GitLab.

def _generate_files(change_id: str, db: Session, progress) -> list[dict]:
    """Whole-file generation from the latest plan, grounded by retrieved excerpts
    + current GitLab contents of the to-modify files. Returns [{path, content}].
    Raises if generation produced nothing. Does NOT persist or push."""
    change = db.execute(
        select(IncomingChange).where(IncomingChange.id == change_id)
    ).scalar_one_or_none()
    if change is None:
        raise RuntimeError(f"unknown change_id: {change_id}")

    plan_row = _latest_code_row(db, change_id)
    plan = json.loads(plan_row.content)
    repo = _latest_indexed_repo(db)
    api_key = _runtime_api_key(db)

    from app.agents.code_files import build_code_files
    from app.services.git_integrator import fetch_current_files

    modify_paths = [
        fc.get("path") for fc in (plan.get("file_changes") or [])
        if isinstance(fc, dict) and fc.get("action") == "modify" and fc.get("path")
    ]
    progress(f"fetching current contents of {len(modify_paths)} files from GitLab")
    current_files = fetch_current_files(db, repo, modify_paths)

    from app.agents.revision_context import assemble_change_context
    ctx = assemble_change_context(db, change_id, api_key)

    n_files = len(plan.get("file_changes") or [])
    progress(f"generating {n_files} files (batched — takes several minutes)")
    files = build_code_files(
        plan=plan,
        change_title=change.title or "Untitled Change",
        documents=ctx["documents"],
        design_summary=_latest_design_summary(db, change_id),
        code_context=build_code_context(db, _change_query(change), repo_id=repo.id),
        current_files=current_files,
        api_key=api_key,
        on_progress=progress,
    )
    if not files:
        raise RuntimeError(
            "file generation produced nothing — check logs (no LLM key, a batch "
            "kept truncating, or the model returned no <<FILE>> blocks)."
        )
    return files


def _latest_iteration(db: Session, change_id: str) -> int:
    """Highest stored file iteration for the change; 0 if none generated yet."""
    from sqlalchemy import func
    return db.execute(
        select(func.max(GeneratedCodeFile.iteration))
        .where(GeneratedCodeFile.change_id == change_id)
    ).scalar() or 0


def _store_generated_files(db: Session, change_id: str, files: list[dict]) -> int:
    """Persist `files` as a new iteration. Tags each with create|modify from the
    latest plan's file_changes (informational). Returns the new iteration number."""
    plan_row = _latest_code_row(db, change_id)
    action_by_path = {}
    if plan_row:
        try:
            plan = json.loads(plan_row.content)
            action_by_path = {
                fc.get("path"): fc.get("action")
                for fc in (plan.get("file_changes") or [])
                if isinstance(fc, dict) and fc.get("path")
            }
        except (json.JSONDecodeError, TypeError):
            pass
    iteration = _latest_iteration(db, change_id) + 1
    for f in files:
        db.add(GeneratedCodeFile(
            change_id=change_id,
            iteration=iteration,
            path=f["path"],
            content=f.get("content", ""),
            action=action_by_path.get(f["path"]),
            code_report_version=plan_row.version if plan_row else None,
        ))
    db.commit()
    return iteration


def _latest_generated_files(db: Session, change_id: str) -> list[dict]:
    """The latest iteration's files as [{path, content}] for the MR push."""
    it = _latest_iteration(db, change_id)
    if it == 0:
        return []
    rows = db.execute(
        select(GeneratedCodeFile)
        .where(GeneratedCodeFile.change_id == change_id, GeneratedCodeFile.iteration == it)
        .order_by(GeneratedCodeFile.path)
    ).scalars().all()
    return [{"path": r.path, "content": r.content} for r in rows]


def _run_codegen_job(change_id: str, db: Session, progress) -> None:
    """Generate the whole-file change set and persist it as a new iteration.
    The MR is NOT opened here — review must pass first (later steps)."""
    files = _generate_files(change_id, db, progress)
    iteration = _store_generated_files(db, change_id, files)
    progress(f"stored {len(files)} files as iteration {iteration}")


# ── Review step (code-quality + security) ───────────────────────────────────
# Two agents review the current generated-file iteration. ANY finding from
# either blocks the merge request (step 3) until corrected. Loop state is DERIVED
# from the artifacts, not stored — see _review_status.

_REVIEWERS = (("code_reviewer", "code_quality"), ("security_reviewer", "security"))


def _review_status(db: Session, change_id: str) -> dict:
    """Derive the review/loop state from the artifacts:
      current_iteration  = max(generated_code_files.iteration)
      reviewed_iteration = max(code_review_reports.iteration)
      findings_count     = total findings in the reviewed iteration
    status: draft (nothing generated) | generated (current not yet reviewed) |
            issues_found (reviewed current has findings) | clean (reviewed, zero).
    can_open_mr is true only when the CURRENT iteration was reviewed with zero
    findings (the merge-request gate, enforced in step 3)."""
    from sqlalchemy import func
    current = _latest_iteration(db, change_id)
    reviewed = db.execute(
        select(func.max(CodeReviewReport.iteration))
        .where(CodeReviewReport.change_id == change_id)
    ).scalar() or 0

    findings_count = 0
    if reviewed:
        rows = db.execute(
            select(CodeReviewReport)
            .where(CodeReviewReport.change_id == change_id, CodeReviewReport.iteration == reviewed)
        ).scalars().all()
        for r in rows:
            try:
                findings_count += len((json.loads(r.content) or {}).get("findings") or [])
            except (json.JSONDecodeError, TypeError):
                pass

    if current == 0:
        status = "draft"
    elif reviewed < current:
        status = "generated"          # current files not reviewed (stale / never)
    elif findings_count > 0:
        status = "issues_found"
    else:
        status = "clean"

    return {
        "current_iteration": current,
        "reviewed_iteration": reviewed,
        "status": status,
        "findings_count": findings_count,
        "can_open_mr": current > 0 and reviewed == current and findings_count == 0,
    }


def _review_current_iteration(change_id: str, db: Session, progress) -> dict:
    """Run both reviewer agents over the CURRENT generated-file iteration and
    persist one CodeReviewReport row each (idempotent for the iteration). Returns
    the derived review status. Shared by the review job and the auto-fix loop.
    Reviewers run sequentially on the one job session — parallelizing would need
    a session per thread (future optimization)."""
    change = db.execute(
        select(IncomingChange).where(IncomingChange.id == change_id)
    ).scalar_one_or_none()
    if change is None:
        raise RuntimeError(f"unknown change_id: {change_id}")
    iteration = _latest_iteration(db, change_id)
    if iteration == 0:
        raise RuntimeError("no generated files to review — run code generation first")

    files = _latest_generated_files(db, change_id)
    api_key = _runtime_api_key(db)
    repo = _latest_indexed_repo(db)
    code_context = build_code_context(db, _change_query(change), repo_id=repo.id) if repo else ""

    from app.agents.revision_context import assemble_change_context
    ctx = assemble_change_context(db, change_id, api_key)

    agent_input = {
        "files": files,
        "change_title": change.title or "",
        "design_summary": _latest_design_summary(db, change_id),
        "documents": ctx["documents"],
        "code_context": code_context,
        "api_key": api_key,
    }

    # Idempotent re-run: clear any prior rows for this exact iteration first.
    db.query(CodeReviewReport).filter(
        CodeReviewReport.change_id == change_id,
        CodeReviewReport.iteration == iteration,
    ).delete()
    db.commit()

    for agent_name, reviewer in _REVIEWERS:
        progress(f"running {reviewer} review over {len(files)} files")
        out = registry.get(agent_name).execute(agent_input, db=db, change_id=change_id)
        db.add(CodeReviewReport(
            change_id=change_id,
            iteration=iteration,
            reviewer=reviewer,
            content=json.dumps(out),
            model_used=(out.get("_meta") or {}).get("model_used"),
        ))
        db.commit()
    return _review_status(db, change_id)


def _run_review_job(change_id: str, db: Session, progress) -> None:
    """Standalone review of the current iteration (the manual 'Run review')."""
    st = _review_current_iteration(change_id, db, progress)
    progress(f"review complete — {st['findings_count']} finding(s) ({st['status']})")


def _reviewed_findings(db: Session, change_id: str, iteration: int) -> list[dict]:
    """Flattened findings from both reviewers for the given reviewed iteration."""
    out: list[dict] = []
    rows = db.execute(
        select(CodeReviewReport)
        .where(CodeReviewReport.change_id == change_id, CodeReviewReport.iteration == iteration)
    ).scalars().all()
    for r in rows:
        try:
            out.extend((json.loads(r.content) or {}).get("findings") or [])
        except (json.JSONDecodeError, TypeError):
            pass
    return out


def _iteration_findings_by_reviewer(db: Session, change_id: str, iteration: int) -> dict:
    """{reviewer: [findings]} for one reviewed iteration (both lenses)."""
    out: dict[str, list] = {"code_quality": [], "security": []}
    rows = db.execute(
        select(CodeReviewReport)
        .where(CodeReviewReport.change_id == change_id, CodeReviewReport.iteration == iteration)
    ).scalars().all()
    for r in rows:
        try:
            fs = (json.loads(r.content) or {}).get("findings") or []
        except (json.JSONDecodeError, TypeError):
            fs = []
        out.setdefault(r.reviewer, []).extend(fs)
    return out


def _finding_key(f: dict) -> tuple:
    """Best-effort identity for matching a finding across iterations (to tell which
    were fixed). The LLM may reword a title, so this is heuristic — a reworded
    finding reads as fixed+new, which is acceptable for the history view."""
    return (
        (f.get("file") or "").strip().lstrip("./").lower(),
        (f.get("title") or "").strip().lower(),
    )


def _review_history(db: Session, change_id: str) -> list[dict]:
    """Per-iteration review timeline with the issues FIXED by each round. For each
    reviewed iteration K, `fixed[lens]` = findings present at K that are gone by
    the next reviewed iteration (i.e. resolved by the fix that produced K+1).
    The final iteration (no successor) has empty `fixed`."""
    from sqlalchemy import func
    iters = sorted(
        i for (i,) in db.execute(
            select(func.distinct(CodeReviewReport.iteration))
            .where(CodeReviewReport.change_id == change_id)
        ).all()
        if i is not None
    )
    per = {it: _iteration_findings_by_reviewer(db, change_id, it) for it in iters}
    history = []
    for idx, it in enumerate(iters):
        cur = per[it]
        nxt = per[iters[idx + 1]] if idx + 1 < len(iters) else None
        entry = {"iteration": it, "reviews": {}, "fixed": {}}
        for lens in ("code_quality", "security"):
            cur_f = cur.get(lens, [])
            entry["reviews"][lens] = cur_f
            if nxt is not None:
                nxt_keys = {_finding_key(g) for g in nxt.get(lens, [])}
                entry["fixed"][lens] = [f for f in cur_f if _finding_key(f) not in nxt_keys]
            else:
                entry["fixed"][lens] = []
        history.append(entry)
    return history


def _findings_map_to_files(findings: list[dict], files: list[dict]) -> bool:
    """True if at least one finding targets a generated file (so a fix pass can
    make progress). When false, auto-fix can't proceed and stops."""
    paths = {f["path"].strip().lstrip("./").strip() for f in files}
    return any((fd.get("file") or "").strip().lstrip("./").strip() in paths for fd in findings)


def _run_fix_job(change_id: str, db: Session, progress) -> None:
    """Auto-fix loop: apply the review findings, RE-REVIEW, and repeat until the
    change is clean (zero findings) or the configured round cap is reached. Each
    round produces a new generated-file iteration and a fresh review. On hitting
    the cap (or findings that don't map to a file) it stops and leaves the change
    in issues_found — the operator can run it again or edit manually. All of this
    runs in the one background job; the UI polls the review report for live state."""
    change = db.execute(
        select(IncomingChange).where(IncomingChange.id == change_id)
    ).scalar_one_or_none()
    if change is None:
        raise RuntimeError(f"unknown change_id: {change_id}")

    if _review_status(db, change_id)["status"] != "issues_found":
        raise RuntimeError("no review findings to fix — run review first")

    from app.agents.code_files import fix_code_files
    api_key = _runtime_api_key(db)
    repo = _latest_indexed_repo(db)
    code_context = build_code_context(db, _change_query(change), repo_id=repo.id) if repo else ""
    max_rounds = max(1, settings.code_review_max_fix_rounds)

    rounds = 0
    while rounds < max_rounds:
        st = _review_status(db, change_id)
        if st["status"] != "issues_found":
            break  # converged → clean

        files = _latest_generated_files(db, change_id)
        findings = _reviewed_findings(db, change_id, st["reviewed_iteration"])
        if not _findings_map_to_files(findings, files):
            progress(
                f"{st['findings_count']} finding(s) don't map to a generated file — "
                "manual edit needed; stopping auto-fix"
            )
            break

        rounds += 1
        progress(f"round {rounds}/{max_rounds}: fixing {st['findings_count']} finding(s)")
        new_files = fix_code_files(
            files=files,
            findings=findings,
            change_title=change.title or "",
            design_summary=_latest_design_summary(db, change_id),
            code_context=code_context,
            api_key=api_key,
            on_progress=progress,
        )
        if not new_files:
            raise RuntimeError(f"auto-fix failed during round {rounds} — check logs")
        new_iteration = _store_generated_files(db, change_id, new_files)

        progress(f"round {rounds}/{max_rounds}: reviewing iteration {new_iteration}")
        _review_current_iteration(change_id, db, progress)

    final = _review_status(db, change_id)
    if final["status"] == "clean":
        progress(f"auto-fix converged after {rounds} round(s) — 0 findings, ready to open MR")
    else:
        progress(
            f"auto-fix stopped after {rounds} round(s) — {final['findings_count']} finding(s) "
            f"remain (round cap {max_rounds}); re-run or edit manually"
        )


def _run_mr_job(change_id: str, db: Session, progress) -> None:
    """Branch/commit/MR push — inside a background AgentJob. Pushes the stored
    generated files (latest iteration); if none were generated yet it falls back
    to generating + storing them first, so a one-click MR still works. The
    CodeMergeRequest row records the outcome either way."""
    change = db.execute(
        select(IncomingChange).where(IncomingChange.id == change_id)
    ).scalar_one_or_none()
    if change is None:
        raise RuntimeError(f"unknown change_id: {change_id}")

    plan_row = _latest_code_row(db, change_id)
    plan = json.loads(plan_row.content)
    repo = _latest_indexed_repo(db)

    from app.services.git_integrator import open_merge_request

    files = _latest_generated_files(db, change_id)
    if not files:
        # Back-compat: no separate generate step was run — generate + store now.
        progress("no stored files — generating before push")
        gen = _generate_files(change_id, db, progress)
        _store_generated_files(db, change_id, gen)
        files = _latest_generated_files(db, change_id)
    else:
        progress(f"pushing stored iteration {_latest_iteration(db, change_id)} ({len(files)} files)")

    branch = f"npci-change-{change_id[:8]}"
    title = f"feat: NPCI change {change_id[:8]} — {(change.title or '')[:60]}"
    grounded = plan.get("_meta", {}).get("grounded") is True
    description = (
        f"## NPCI change `{change_id}`\n\n"
        f"AI-generated by the partner platform from the implementation plan "
        f"(v{plan_row.version}, {'repository-grounded' if grounded else 'spec-grounded'}). "
        f"**Review before merging** — this is a starting point, not a vetted change.\n\n"
        f"Files: {len(files)}."
    )

    progress(f"pushing {len(files)} files — branch, commit, merge request")
    row = CodeMergeRequest(
        change_id=change_id, repo_id=repo.id, code_report_version=plan_row.version,
        branch=branch, files_count=len(files),
    )
    try:
        result = open_merge_request(
            db, repo, branch_name=branch, files=files, title=title, description=description,
        )
    except Exception as exc:  # noqa: BLE001 — record the failure for the UI
        row.status = "error"
        row.last_error = str(exc)[:500]
        db.add(row)
        db.commit()
        raise RuntimeError(f"merge request failed: {exc}")

    row.mr_url = result["mr_url"]
    row.mr_iid = result["mr_iid"]
    row.commit_sha = result["commit_sha"]
    row.status = "opened"
    db.add(row)
    db.commit()


def _codegen_preconditions(db: Session, change_id: str) -> None:
    """Shared 4xx checks for generate + MR: change exists, a readable plan exists,
    and a repo is indexed (the file-gen grounds + the MR pushes against it)."""
    change = db.execute(
        select(IncomingChange).where(IncomingChange.id == change_id)
    ).scalar_one_or_none()
    if change is None:
        raise HTTPException(status_code=404, detail=f"unknown change_id: {change_id}")
    plan_row = _latest_code_row(db, change_id)
    if plan_row is None:
        raise HTTPException(status_code=400, detail="run the code plan first — no plan to generate from")
    try:
        json.loads(plan_row.content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="stored plan is unreadable")
    if _latest_indexed_repo(db) is None:
        raise HTTPException(
            status_code=400,
            detail="no indexed repository — register + index one under Settings first",
        )


@router.post("/changes/{change_id}/code/generate", status_code=202)
def generate_code(
    change_id: str,
    bg: BackgroundTasks,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generate the whole-file change set from the latest plan and store it as a
    new iteration (a reviewable artifact) — does NOT open an MR. Returns 202 + a
    job id; poll GET /changes/{id}/jobs/codegen/latest, then GET .../code/files."""
    _codegen_preconditions(db, change_id)
    from .jobs import start_job
    return start_job(
        db, bg, change_id=change_id, kind="codegen",
        runner=lambda jdb, progress: _run_codegen_job(change_id, jdb, progress),
    )


@router.get("/changes/{change_id}/code/files")
def list_generated_files(
    change_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The current (latest) iteration's generated files — path, action, size.
    Content is omitted here to keep the list light; iteration is returned so the
    UI can show staleness. 200 with empty list when nothing generated yet."""
    iteration = _latest_iteration(db, change_id)
    rows = []
    if iteration:
        rows = db.execute(
            select(GeneratedCodeFile)
            .where(
                GeneratedCodeFile.change_id == change_id,
                GeneratedCodeFile.iteration == iteration,
            )
            .order_by(GeneratedCodeFile.path)
        ).scalars().all()
    return {
        "change_id": change_id,
        "iteration": iteration,
        "files": [
            {"path": r.path, "action": r.action, "size_bytes": len(r.content or "")}
            for r in rows
        ],
    }


@router.post("/changes/{change_id}/code/review", status_code=202)
def review_code(
    change_id: str,
    bg: BackgroundTasks,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Run code-quality + security review over the current generated-file
    iteration. Returns 202 + a job id; poll GET /changes/{id}/jobs/review/latest,
    then GET .../code/review/report. 400 if nothing has been generated."""
    change = db.execute(
        select(IncomingChange).where(IncomingChange.id == change_id)
    ).scalar_one_or_none()
    if change is None:
        raise HTTPException(status_code=404, detail=f"unknown change_id: {change_id}")
    if _latest_iteration(db, change_id) == 0:
        raise HTTPException(status_code=400, detail="generate the code first — nothing to review")

    from .jobs import start_job
    return start_job(
        db, bg, change_id=change_id, kind="review",
        runner=lambda jdb, progress: _run_review_job(change_id, jdb, progress),
    )


@router.post("/changes/{change_id}/code/fix", status_code=202)
def fix_code(
    change_id: str,
    bg: BackgroundTasks,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Apply the latest review findings to the code (re-gen → new iteration).
    Only available when the current iteration was reviewed and has findings
    (status == issues_found). Returns 202 + a job id; poll
    GET /changes/{id}/jobs/fix/latest, then re-run review."""
    change = db.execute(
        select(IncomingChange).where(IncomingChange.id == change_id)
    ).scalar_one_or_none()
    if change is None:
        raise HTTPException(status_code=404, detail=f"unknown change_id: {change_id}")
    st = _review_status(db, change_id)
    if st["status"] != "issues_found":
        raise HTTPException(
            status_code=400,
            detail="apply fixes is only available after a review found issues "
                   f"(current status: {st['status']})",
        )

    from .jobs import start_job
    return start_job(
        db, bg, change_id=change_id, kind="fix",
        runner=lambda jdb, progress: _run_fix_job(change_id, jdb, progress),
    )


@router.get("/changes/{change_id}/code/review/report")
def get_code_review(
    change_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Merged findings of the latest reviewed iteration + derived loop status.
    Always 200: an un-reviewed change returns status/empty reviews so the UI can
    render the right call-to-action."""
    st = _review_status(db, change_id)
    reviews = []
    if st["reviewed_iteration"]:
        rows = db.execute(
            select(CodeReviewReport)
            .where(
                CodeReviewReport.change_id == change_id,
                CodeReviewReport.iteration == st["reviewed_iteration"],
            )
            .order_by(CodeReviewReport.reviewer)
        ).scalars().all()
        for r in rows:
            try:
                content = json.loads(r.content) or {}
            except (json.JSONDecodeError, TypeError):
                content = {"summary": "(unreadable)", "findings": []}
            reviews.append({
                "reviewer": r.reviewer,
                "model_used": r.model_used,
                "generated_at": r.generated_at.isoformat() if r.generated_at else None,
                "summary": content.get("summary"),
                "findings": content.get("findings") or [],
            })
    return {"change_id": change_id, **st, "reviews": reviews}


@router.get("/changes/{change_id}/code/review/history")
def get_code_review_history(
    change_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Per-iteration review timeline: the code-quality + security findings at each
    iteration and which of them were FIXED by the following round. Drives the
    'fixed issues per iteration' view. Always 200."""
    return {"change_id": change_id, "iterations": _review_history(db, change_id)}


@router.post("/changes/{change_id}/code/merge-request", status_code=202)
def open_code_mr(
    change_id: str,
    bg: BackgroundTasks,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Push the reviewed-clean generated files as a merge request; returns 202 +
    a job id. Gated: the CURRENT generated-file iteration must have passed both
    reviews with zero findings (the code-review/security-review loop). Poll
    GET /changes/{id}/jobs/mr/latest, then GET .../code/merge-request."""
    change = db.execute(
        select(IncomingChange).where(IncomingChange.id == change_id)
    ).scalar_one_or_none()
    if change is None:
        raise HTTPException(status_code=404, detail=f"unknown change_id: {change_id}")

    plan_row = _latest_code_row(db, change_id)
    if plan_row is None:
        raise HTTPException(status_code=400, detail="run the code plan first — no plan to push")
    try:
        json.loads(plan_row.content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="stored plan is unreadable")

    if _latest_indexed_repo(db) is None:
        raise HTTPException(
            status_code=400,
            detail="no indexed repository — register + index one under Settings first",
        )

    # Review gate: no MR until the CURRENT iteration is reviewed with zero
    # findings. Any finding (any severity) sends it back to the fix step.
    st = _review_status(db, change_id)
    if not st["can_open_mr"]:
        status = st["status"]
        if status == "draft":
            detail = "generate the code first — nothing to review or push"
        elif status == "generated":
            detail = (
                "the current files have not been reviewed — run code review "
                "(and pass) before opening a merge request"
            )
        elif status == "issues_found":
            detail = (
                f"review found {st['findings_count']} issue(s) — apply fixes and "
                "re-review until clean before opening a merge request"
            )
        else:
            detail = "code review must pass (zero findings) before opening a merge request"
        raise HTTPException(status_code=409, detail=detail)

    from .jobs import start_job
    return start_job(
        db, bg, change_id=change_id, kind="mr",
        runner=lambda jdb, progress: _run_mr_job(change_id, jdb, progress),
    )


@router.get("/changes/{change_id}/code/merge-request")
def get_code_mr(
    change_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the latest merge request opened for this change. 404 → none yet."""
    row = db.execute(
        select(CodeMergeRequest)
        .where(CodeMergeRequest.change_id == change_id)
        .order_by(CodeMergeRequest.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="no merge request yet")
    return _mr_view(row)
