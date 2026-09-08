# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""THE ACCEPTANCE TEST for domain neutrality.

    Install this for a library-lending network, drive one change end to end,
    and open the generated sign-off PDF. Nothing on screen or in the document
    should say "UPI", "VPA", "NPCI", "Bank", "PSP" or name a real financial
    institution.

Every other domain test checks one layer. This one drives a change from arrival
to signed document through the REAL code path — the A2A change handler, the
feasibility agent, the readiness declaration, the cert-response handler, the
dashboard read models and the PDF route — under DOMAIN_PACK=nlln, then inspects
what a user would actually see.

WHY THIS EXISTS SEPARATELY. Layer tests cannot catch a composition leak: each
one passes while the assembled output still carries the wrong vocabulary,
because nothing ever reads the rendered result. The leaks this shape of test
catches live in the PDF route's CALLER rather than in the PDF builder the
layer tests exercise — feature-name fallbacks, identity fallbacks, and
hardcoded download filenames.
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest
from reportlab import rl_config

from app.a2a_common.handlers import TaskReceiveRequest, handle_change_communication
from app.a2a_common.handlers._background import _auto_feasibility
from app.a2a_common.handlers.cert_test_response import handle_cert_test_response
from app.core.domain import clear_cache
from app.models import FeasibilityReport, IncomingChange

PACKS = pathlib.Path(__file__).resolve().parents[1] / "app" / "packs"

# What must NOT appear anywhere a user can see. "Bank" is included as a whole
# word per the acceptance criterion; it is the one most likely to survive,
# because it reads as generic English until you are a library.
FORBIDDEN = ["UPI", "VPA", "NPCI", "PSP", "TPAP", "IFSC",
             "National Payments", "Payer", "Payee", "Remitter", "Beneficiary"]
FORBIDDEN_WORDS = ["Bank", "bank", "payment", "Payment"]

# Real institutions that must never appear in shipped defaults.
REAL_INSTITUTIONS = ["SBI", "HDFC", "ICICI", "Axis", "Kotak", "PhonePe", "Paytm"]


def scan(text: str) -> list[str]:
    """Domain terms present in user-visible text."""
    hits = [t for t in FORBIDDEN if t in text]
    hits += [w for w in FORBIDDEN_WORDS if re.search(rf"\b{w}\b", text)]
    hits += [i for i in REAL_INSTITUTIONS if re.search(rf"\b{i}\b", text)]
    return sorted(set(hits))


@pytest.fixture()
def library_deployment(monkeypatch):
    """A deployment installed for the library-lending network."""
    monkeypatch.setenv("DOMAIN_PACK", str(PACKS / "nlln" / "nlln.yaml"))
    clear_cache()
    old = rl_config.pageCompression
    rl_config.pageCompression = 0   # so the PDF's drawn text is readable here
    yield
    rl_config.pageCompression = old
    clear_cache()


def drive_change_to_certified(db) -> IncomingChange:
    """Arrival → feasibility → readiness → certified, via the real handlers."""
    # 1. The authority publishes a change over A2A.
    body = TaskReceiveRequest(
        task_type="change_communication",
        change_id="NLLC-114",
        payload={
            "change_id": "NLLC-114",
            "title": "Recall notification window",
            "documents": [{
                "doc_type": "brd",
                "content": "Lending libraries must acknowledge a recall within 48 hours. "
                           "Adds RespRecallNotify with a mandatory dueDate.",
            }],
        },
    )
    local_id = handle_change_communication(body, db)["local_id"]

    # 2. The feasibility agent evaluates it against the partner profile.
    _auto_feasibility(local_id)

    change = db.get(IncomingChange, local_id)

    # 3. The partner declares readiness with a role and its identifiers. Stored
    #    the way the readiness endpoint stores them, then echoed by the PDF.
    change.cert_status = "ready_for_certification"
    db.commit()

    # 4. The authority's cert orchestrator publishes results back over A2A.
    handle_cert_test_response(
        TaskReceiveRequest(
            task_type="cert_test_response",
            change_id="NLLC-114",
            payload={
                "change_id": "NLLC-114",
                "cert_run_id": "TR-NLLN-114",
                "feature_name": "Recall notification window",
                "role": "LENDING_LIBRARY",
                "total": 2, "passed": 2, "failed": 0,
                "completed_at": "2026-05-04T09:00:00Z",
                "results": [
                    {"tc_id": "LE_01", "title": "Recall acknowledged within window",
                     "initiated_by": "NPCI", "status": "PASS",
                     "expected_code": "000", "actual_code": "000"},
                    {"tc_id": "LE_02", "title": "Recall refused for reference copy",
                     "initiated_by": "BANK", "status": "PASS",
                     "expected_code": "E14", "actual_code": "E14"},
                ],
            },
        ),
        db,
    )

    change = db.get(IncomingChange, local_id)
    summary = json.loads(change.cert_summary or "{}")
    summary.update({
        "role": "LENDING_LIBRARY",
        "test_data": {"isbn": "9780000000001", "shelf_location": "STACK-3-A",
                      "org_id": "anna-library"},
        "feature": "Recall notification window",
    })
    change.cert_summary = json.dumps(summary)
    change.cert_status = "certified"
    change.cert_status_history = json.dumps({"certified": "2026-05-04T09:00:00Z"})
    db.commit()
    return db.get(IncomingChange, local_id)


# ── The change actually completes ────────────────────────────────────────────

def test_a_change_drives_end_to_end_under_a_library_pack(library_deployment, db_session):
    """Before checking vocabulary: the lifecycle must genuinely work.

    A test that only greps for absent words would pass just as happily against
    a platform that had stopped functioning.
    """
    change = drive_change_to_certified(db_session)
    assert change.cert_status == "certified"
    assert db_session.query(FeasibilityReport).filter_by(change_id=change.id).count() == 1
    summary = json.loads(change.cert_summary)
    assert summary["total"] == 2 and summary["passed"] == 2
    assert len(summary.get("cases") or []) == 2


# ── The document ─────────────────────────────────────────────────────────────

def download_signoff(db, change_id):
    """Fetch the sign-off through the REAL endpoint, not the PDF builder.

    This distinction is the whole point. An earlier draft of this test called
    `build_signoff_pdf` directly and passed — while the endpoint that calls it
    still defaulted `feature` to "UPI Product", `partner_name` to "Partner
    Bank", and the download filename to "NPCI_Certification_Signoff_". Every
    one of those lives in the CALLER, so a test that skips the caller cannot
    see them. Drive the route.
    """
    from app.api.dashboard.certification import download_cert_signoff_pdf

    return download_cert_signoff_pdf(change_id=change_id, user=None, db=db)


def test_the_signed_pdf_says_nothing_about_payments(library_deployment, db_session):
    """Open the generated sign-off and read it."""
    change = drive_change_to_certified(db_session)
    resp = download_signoff(db_session, change.id)
    drawn = " ".join(re.findall(r"\((.*?)\)\s*Tj", resp.body.decode("latin-1")))

    hits = scan(drawn)
    assert not hits, f"the signed sign-off PDF contains: {hits}"

    # And it says the right things — absence alone would also be satisfied by
    # a document that had lost all its content.
    for expected in ["National Library Lending Council", "Lending Library",
                     "ISBN", "NLLC", "LIBRARY"]:
        assert expected in drawn, f"sign-off is missing {expected!r}"


def test_the_download_filename_names_no_third_party(library_deployment, db_session):
    """The filename is the first thing a user sees, before opening anything.

    It was hardcoded to "NPCI_Certification_Signoff_", which also made
    TRADEMARKS.md's claim about a neutral default prefix untrue.
    """
    change = drive_change_to_certified(db_session)
    disposition = download_signoff(db_session, change.id).headers["content-disposition"]
    assert not scan(disposition), f"download filename contains: {scan(disposition)}"
    assert "NLLN" in disposition


def test_a_summary_without_a_feature_name_does_not_invent_a_payments_one(
    library_deployment, db_session,
):
    """Exercises the `feature` fallback in the PDF route.

    The authority's cert lifecycle ships two messages on one wire, and the
    second (the sign-off payload) does not carry `feature_name`. So a summary
    with no feature is a REAL state, not a contrived one — and the route
    defaulted it to the literal "UPI Product". The happy path never reaches
    this line, which is why it survived every earlier test.
    """
    change = drive_change_to_certified(db_session)
    summary = json.loads(change.cert_summary)
    summary.pop("feature", None)
    change.cert_summary = json.dumps(summary)
    db_session.commit()

    drawn = " ".join(re.findall(
        r"\((.*?)\)\s*Tj", download_signoff(db_session, change.id).body.decode("latin-1")))
    assert not scan(drawn), f"feature fallback leaked: {scan(drawn)}"
    assert "NLLN Lending-Protocol Certification" in drawn


def test_an_unconfigured_partner_identity_is_not_a_bank(library_deployment, db_session):
    """With no profile and no partner_name, the PDF still names someone.

    That last-resort literal was "Partner Bank" — so a library network that had
    merely not filled in its profile got a financial institution printed on its
    signed document.
    """
    change = drive_change_to_certified(db_session)
    drawn = " ".join(re.findall(
        r"\((.*?)\)\s*Tj", download_signoff(db_session, change.id).body.decode("latin-1")))
    assert not scan(drawn)
    assert "The Partner" in drawn, "the fallback identity vanished entirely"


# ── The screens ──────────────────────────────────────────────────────────────

def test_the_role_picker_offers_only_library_roles(library_deployment, db_session):
    """What the Declare Ready dialog renders."""
    from app.api.dashboard.domain import get_domain

    payload = get_domain(user=None)
    hits = scan(json.dumps(payload))
    assert not hits, f"the role picker payload contains: {hits}"
    assert [r["key"] for r in payload["roles"]] == [
        "LENDING_LIBRARY", "BORROWING_LIBRARY", "MEMBER_REGISTRY",
    ]


def test_the_feasibility_report_on_screen_is_clean(library_deployment, db_session):
    """The largest block of generated prose a user reads in the UI."""
    change = drive_change_to_certified(db_session)
    report = db_session.query(FeasibilityReport).filter_by(change_id=change.id).one()

    text = " ".join(
        str(getattr(report, col.name) or "")
        for col in report.__table__.columns
    )
    hits = scan(text)
    assert not hits, f"the feasibility report shown on screen contains: {hits}"


def test_every_agent_prompt_this_run_used_is_clean(library_deployment):
    """The instructions behind the generated prose, not just its output."""
    from app.agents import prompts as prompts_mod

    prompts_mod.clear_cache()
    for name in ["feasibility.md", "design.md", "test.md", "code.md",
                 "security_reviewer.md", "negotiation.md"]:
        hits = scan(prompts_mod.load_prompt(name))
        assert not hits, f"{name} as rendered for a library deployment contains: {hits}"


# ── The counter-check ────────────────────────────────────────────────────────

def test_the_same_run_under_the_upi_pack_still_says_upi(monkeypatch, db_session):
    """Proof the vocabulary moved into the pack rather than being deleted.

    Without this, every assertion above would also pass against a platform that
    had simply had its domain wording stripped out and replaced with nothing.
    """
    monkeypatch.setenv("DOMAIN_PACK", str(PACKS / "upi" / "upi.yaml"))
    clear_cache()
    old = rl_config.pageCompression
    rl_config.pageCompression = 0
    try:
        from app.agents import prompts as prompts_mod
        from app.api.dashboard.domain import get_domain
        from app.services.cert_signoff_pdf import build_signoff_pdf

        prompts_mod.clear_cache()
        assert "UPI" in prompts_mod.load_prompt("feasibility.md")
        assert [r["key"] for r in get_domain(user=None)["roles"]][0] == "PAYER_PSP"

        pdf = build_signoff_pdf(
            partner_name="Meridian Commercial Bank", feature="UPI Circle",
            run_id="TR-1", executed_at="2026-05-04T09:00:00Z", role="PAYER_PSP",
            test_data={"payer_vpa": "test@examplebank"},
            cases=[{"test_case_id": "PR_01", "initiated_by": "NPCI", "status": "PASS"}],
            summary={"total": 1, "passed": 1},
        )
        drawn = " ".join(re.findall(r"\((.*?)\)\s*Tj", pdf.decode("latin-1")))
        assert "National Payments Corporation of India" in drawn
        assert "Payer VPA" in drawn
    finally:
        rl_config.pageCompression = old
        clear_cache()
