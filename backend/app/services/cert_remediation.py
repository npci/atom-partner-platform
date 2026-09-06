# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Cert-verdict remediation (CERT-5) — the bank's half of the defect loop.

A `real_defect` verdict opens (or appends to) the change's ONE open fix round,
converts the verdicts into review-shaped findings, and attempts the fix. The
`code/fix` guard (`api/dashboard/code.py::_findings_map_to_files`) is
deliberately NOT touched: its contract is "apply the latest REVIEW findings",
and a cert finding — which names an API and an xpath, never a source file —
would be rejected by it by construction. This module is the separate runner
that reuses the same `fix_code_files` agent without perverting the review
flow.

`verdicts_to_findings` leaves `file` EMPTY rather than guessing. Consequence,
recorded honestly: until something maps a cert defect to the generated file
that implements it (a capability question — see ITA Stage 2), the automated
fix cannot run, and `run_fix_round` parks the round at `awaiting_manual_fix`
instead of pretending. The operator fixes, marks the round fixed, and the
approval endpoint — the ONLY caller of `send_cert_fix_notification` — closes
the loop.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import CertFixRound

logger = logging.getLogger(__name__)

__all__ = ["OPEN_STATUSES", "open_round", "verdicts_to_findings", "run_fix_round"]

# Statuses at which a round still accepts more failing cases.
OPEN_STATUSES = ("open", "fixing", "awaiting_manual_fix")


def open_round(db: Session, *, change_id: str, cflow_id: str | None,
               case_id: str, verdict: dict) -> CertFixRound:
    """Append this failing case to the change's OPEN fix round, creating it if
    none exists. One verdict per failing case arrives; one round carries them
    all — five failures are one fix job.

    CONCURRENCY. The authority sends one verdict per failing case and they can
    land at the same instant. As a bare select-then-insert this raced: both
    calls saw "no open round", both created round 1, and one batch became two
    jobs. Now the found row is LOCKED for update (serialising the append), and
    the create path is protected by UNIQUE(change_id, round_number) so the
    loser of an insert race gets an IntegrityError and retries into the
    winner's round instead of duplicating it.

    `with_for_update()` is dropped by the SQLite dialect, which is correct
    there — the test harness is single-connection.
    """
    for attempt in (1, 2):
        committed = False
        try:
            rnd = db.execute(
                select(CertFixRound)
                .where(CertFixRound.change_id == change_id,
                       CertFixRound.status.in_(OPEN_STATUSES))
                .order_by(CertFixRound.round_number)
                .with_for_update()
            ).scalars().first()
            if rnd is None:
                # max+1, not count+1: a deleted or renumbered row must not
                # hand out a number that is already taken.
                highest = db.execute(
                    select(func.max(CertFixRound.round_number))
                    .where(CertFixRound.change_id == change_id)
                ).scalar() or 0
                rnd = CertFixRound(change_id=change_id, cflow_id=cflow_id,
                                   round_number=highest + 1,
                                   verdict_case_ids=[], verdicts=[])
                db.add(rnd)
                db.flush()   # surfaces the insert race HERE, where we can retry
            if case_id and case_id not in (rnd.verdict_case_ids or []):
                # Reassign, don't mutate: JSON columns do not track in-place changes.
                rnd.verdict_case_ids = list(rnd.verdict_case_ids or []) + [case_id]
                rnd.verdicts = list(rnd.verdicts or []) + [dict(verdict or {})]
            if cflow_id and not rnd.cflow_id:
                rnd.cflow_id = cflow_id
            db.commit()
            committed = True
            return rnd
        except IntegrityError:
            if attempt == 2:
                raise
            logger.info(
                "cert_fix_round for change=%s created concurrently — "
                "retrying the append into the existing round", change_id,
            )
        finally:
            # Rollback on any failure, named or not, without a broad except.
            if not committed:
                db.rollback()
    raise RuntimeError("unreachable")  # pragma: no cover


def verdicts_to_findings(verdicts: list[dict]) -> list[dict]:
    """Convert verdict payloads to review-finding shape. PURE.

    `file` stays EMPTY — a cert verdict names an API and an xpath, never a
    source file, and inventing a path would send the fix agent at the wrong
    code with full confidence. The API + xpath + broken rule go in the
    description, which is what a human (or a future mapper) needs.
    """
    findings: list[dict] = []
    for verdict in verdicts or []:
        case_id = verdict.get("test_case_id") or verdict.get("case_id") or ""
        failures = verdict.get("assertion_failures") or []
        if not failures:
            findings.append({
                "file": "", "severity": "major",
                "title": f"cert case {case_id} failed",
                "description": (
                    f"Certification case {case_id} failed: expected response "
                    f"code {verdict.get('expected_code')!r}, observed "
                    f"{verdict.get('actual_code')!r}."),
            })
            continue
        for failure in failures:
            findings.append({
                "file": "", "severity": "major",
                "title": f"cert case {case_id}: {failure.get('kind')} violated "
                         f"at {failure.get('field')}",
                "description": (
                    f"Certification case {case_id}: field {failure.get('field')!r} "
                    f"broke its own registry constraint "
                    f"({failure.get('kind')}: {failure.get('expected')!r}) — "
                    f"{failure.get('reason')}"),
            })
    return findings


def run_fix_round(round_id: str) -> None:
    """Background worker: attempt the automated fix for a round.

    Opens its own session (runs off the request path). Honest by design:
    with unmapped findings (`file` empty — today, all of them) the fix agent
    cannot target anything, so the round parks at `awaiting_manual_fix` with
    the findings recorded in `fix_note` for the operator. When a mapping step
    exists, the fix path (fix_code_files → new iteration → re-review →
    awaiting_approval) lights up behind this same worker.
    """
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        rnd = db.get(CertFixRound, round_id)
        if rnd is None or rnd.status not in ("open", "fixing"):
            return
        findings = verdicts_to_findings(rnd.verdicts or [])
        if not any(f.get("file") for f in findings):
            rnd.status = "awaiting_manual_fix"
            rnd.fix_note = (
                f"{len(findings)} finding(s) name an API/xpath, not a source "
                "file — automated fix cannot target them. Fix manually, then "
                "mark the round fixed to enable approval.")
            db.commit()
            logger.info("cert_fix_round %s: %s", round_id, rnd.fix_note)
            return
        # Mapped findings would flow into fix_code_files here (new iteration,
        # re-review, awaiting_approval). Unreachable until a mapper exists —
        # left explicit rather than silently absent.
        rnd.status = "fixing"
        db.commit()
    except Exception:  # noqa: BLE001 — a worker crash must not take the loop down
        logger.exception("cert_fix_round %s: fix worker failed", round_id)
        db.rollback()
    finally:
        db.close()
