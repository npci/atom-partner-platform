# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Certification Sign-off PDF generator.

Rendered on demand by the partner dashboard's `cert-signoff.pdf` endpoint
after a cert run completes. Layout: masthead with the issuer's logo and name, a
congratulatory paragraph naming the certified partner, a metadata block,
the per-TC results table, and a signature/date footer.

EVERY NAME ON THIS DOCUMENT COMES FROM THE ACTIVE DOMAIN PACK. This is the one
artifact that leaves the building, so it is the one place where leftover
vocabulary is not an internal embarrassment but a false statement about who
certified what. See `_signoff()` below.

Pure-python via reportlab — no headless browser, no external CSS.
"""
from __future__ import annotations

import html
import io
import os
from datetime import datetime, timezone
from typing import Any

from reportlab import rl_config
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.core.domain import Signoff, get_active_pack
from app.core.test_status import TestStatus, normalise

# Defense-in-depth against reportlab's <img src="…"> SSRF (CVE-2020-28463): reportlab's
# stock defaults trust http/https/ftp AND every host (trustedHosts=None), so any markup we
# render that reaches an <img>/link tag would fetch a remote URL server-side. This PDF only
# ever embeds a LOCAL logo, so we drop the remote schemes.
#
# 'file' is dropped too, which the earlier revision kept. Blocking the network
# is not the whole threat: `<img src="file:///etc/…">` in injected markup is a
# server-side LOCAL file read whose contents land in a PDF the partner
# downloads. The logo is loaded by path through `Image(...)` rather than by
# markup URL, so nothing this module legitimately renders needs the scheme.
rl_config.trustedSchemes = ["data"]
rl_config.trustedHosts = []

# Resolve the logo relative to this file so it works regardless of cwd.
# Operator-supplied and untracked: we cannot ship an institution's mark in the
# repo (trademark), so `assets/` is gitignored and the header simply omits the
# logo when the file is absent — see the os.path.exists guard below.
_LOGO_PATH = os.environ.get("BRAND_LOGO_PATH") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets", "brand_logo.png",
)

# House palette — mirror the on-screen results panel so print matches web.
_NAVY   = colors.HexColor("#0f172a")   # slate-900
_INK    = colors.HexColor("#1e293b")   # slate-800
_MUTED  = colors.HexColor("#475569")   # slate-600
_HAIR   = colors.HexColor("#e2e8f0")   # slate-200 borders
_STRIPE = colors.HexColor("#f8fafc")   # slate-50 alt row
_OK     = colors.HexColor("#059669")   # emerald-600
_FAIL   = colors.HexColor("#dc2626")   # red-600
_WARN   = colors.HexColor("#b45309")   # amber-700
_TEAL   = colors.HexColor("#0d9488")   # partner initiator
_SKY    = colors.HexColor("#0369a1")   # timeout scenario


# ── Domain identity ──────────────────────────────────────────────────────────
# This is the only artifact that leaves the building, so every name on it comes
# from the active domain pack rather than from this file — programme name,
# signatory, copyright line and accent colour alike. Hardcoding any of them here
# puts one deployment's identity on every other deployment's signed document.
#
# Resolved per call, not at import. The registry caches the parse so this costs
# a dict lookup, and resolving late means a process (or a test) that re-points
# DOMAIN_PACK renders the new identity instead of one frozen at import.

def _signoff() -> Signoff:
    return get_active_pack().signoff


def _accent() -> colors.Color:
    return colors.HexColor(_signoff().accent)


def _masthead() -> str:
    """Running header: "<issuer> · <programme> Sign-off", issuer permitting.

    The generic pack ships an empty issuer precisely so an unconfigured
    deployment does not print someone else's name across the top of every page
    of a document it signs.
    """
    s = _signoff()
    title = f"{s.programme} Sign-off".strip()
    return f"{s.issuer} · {title}" if s.issuer else title


def _initiator_label(wire_token: str) -> str:
    """Map a PINNED wire token to its printable form.

    `initiated_by` travels over A2A as NPCI / BANK and those values are matched
    byte-for-byte by the counterparty — they are not renamed, here or anywhere.
    This maps them for DISPLAY only. Anything unrecognised is passed through
    rather than guessed at, so an unexpected token shows up in the document
    instead of being silently relabelled as one of the two known ones.
    """
    s = _signoff()
    return {"NPCI": s.authority_short, "BANK": s.partner_short}.get(wire_token, wire_token)


def _fmt_dt(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%d %b %Y, %H:%M %Z").strip()
    except Exception:  # noqa: BLE001
        return iso


def _status_colour(s: str) -> colors.Color:
    # Named outcome constants rather than bare literals — see core/test_status.py
    # (these are certification test results, not credentials).
    u = (s or "").upper()
    if u == TestStatus.PASS: return _OK
    if u == TestStatus.FAIL: return _FAIL
    if u == "TIMEOUT":       return _WARN
    return _MUTED


def _scenario_label(s: str) -> str:
    u = (s or "success").lower()
    return {"success": "Success", "failure": "Failure", "timeout": "Timeout"}.get(u, u.title())


def _scenario_colour(s: str) -> colors.Color:
    u = (s or "success").lower()
    if u == "failure": return _WARN
    if u == "timeout": return _SKY
    return _MUTED


def _draw_page_frame(cnv: canvas.Canvas, doc) -> None:
    """Header/footer per page — thin rule + centred run-id/page-number."""
    width, height = A4
    cnv.saveState()
    # Header rule
    cnv.setStrokeColor(_HAIR)
    cnv.setLineWidth(0.6)
    cnv.line(20 * mm, height - 15 * mm, width - 20 * mm, height - 15 * mm)
    cnv.setFillColor(_MUTED)
    cnv.setFont("Helvetica", 8)
    cnv.drawString(20 * mm, height - 12 * mm, _masthead())
    cnv.drawRightString(width - 20 * mm, height - 12 * mm,
                        f"Page {cnv.getPageNumber()}")
    # Footer rule
    cnv.setStrokeColor(_HAIR)
    cnv.line(20 * mm, 15 * mm, width - 20 * mm, 15 * mm)
    cnv.setFont("Helvetica", 7.5)
    cnv.setFillColor(_MUTED)
    cnv.drawString(20 * mm, 11 * mm,
                   "This document is a system-generated sign-off. "
                   "Non-repudiation guaranteed by the A2A audit log.")
    issuer = _signoff().issuer
    if issuer:
        cnv.drawRightString(width - 20 * mm, 11 * mm, f"© {issuer}")
    cnv.restoreState()


def build_signoff_pdf(
    *,
    partner_name: str,
    feature: str,
    run_id: str,
    executed_at: str | None,
    role: str | None,
    test_data: dict[str, Any] | None,
    cases: list[dict[str, Any]],
    summary: dict[str, Any],
) -> bytes:
    """Return the sign-off PDF as bytes.

    `cases` is the frontend-shaped list: each row has `test_case_id`, `title`,
    `api`, `initiated_by`, `scenario`, `expected_code`, `actual_code`,
    `status`, `duration_ms`, `remarks`.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=22 * mm,  bottomMargin=20 * mm,
        # PDF document properties, not body text — these show in a reader's
        # title bar and in file-manager metadata, so they leak the issuer's
        # name into places a body-text sweep never looks.
        title=f"{_signoff().programme} Sign-off — {feature}",
        author=_signoff().issuer or _signoff().signatory,
    )

    styles = getSampleStyleSheet()
    h_title = ParagraphStyle(
        "TitleCert", parent=styles["Heading1"], fontName="Helvetica-Bold",
        fontSize=18, leading=22, textColor=_NAVY, spaceAfter=2, alignment=1,
    )
    h_sub = ParagraphStyle(
        "SubCert", parent=styles["BodyText"], fontName="Helvetica",
        fontSize=10, leading=13, textColor=_MUTED, alignment=1, spaceAfter=14,
    )
    h_section = ParagraphStyle(
        "SectionCert", parent=styles["Heading3"], fontName="Helvetica-Bold",
        fontSize=11, leading=14, textColor=_NAVY, spaceBefore=10, spaceAfter=6,
    )
    body = ParagraphStyle(
        "BodyCert", parent=styles["BodyText"], fontName="Helvetica",
        fontSize=10, leading=15, textColor=_INK, spaceAfter=8,
    )
    body_c = ParagraphStyle(
        "BodyCentre", parent=body, alignment=1, spaceAfter=10,
    )
    caption = ParagraphStyle(
        "CapCert", parent=styles["BodyText"], fontName="Helvetica",
        fontSize=8.5, leading=11, textColor=_MUTED, alignment=1,
    )
    stamp = ParagraphStyle(
        "StampCert", parent=styles["BodyText"], fontName="Helvetica-Oblique",
        fontSize=9.5, leading=13, textColor=_MUTED,
    )

    story: list[Any] = []

    # ── Masthead: logo + issuing authority + certificate title ──────────
    if os.path.exists(_LOGO_PATH):
        img = Image(_LOGO_PATH, width=55 * mm, height=17 * mm)
        img.hAlign = "CENTER"
        story.append(img)
        story.append(Spacer(1, 6))
    _s = _signoff()
    if _s.issuer:
        story.append(Paragraph(html.escape(_s.issuer), caption))
    if _s.signatory:
        story.append(Paragraph(html.escape(_s.signatory), caption))
    story.append(Spacer(1, 12))
    story.append(Paragraph("Certification Sign-off", h_title))
    story.append(Paragraph(
        # Escaped like every sibling field. `feature` is read from
        # `cert_summary`, a free-form dict persisted verbatim by the
        # `cert_test_response` A2A handler, and reportlab parses a Paragraph as
        # RML markup — so this is the one interpolation in the file where
        # stored input becomes markup rather than text. `<font>` rendered;
        # malformed markup raised ParaParser's ValueError and 500'd the
        # sign-off download for good.
        #
        # That dict now arrives ONLY from the authority over A2A; it was also
        # accepted straight from the browser via POST /cert-mock-complete until
        # that endpoint was removed. The escape stays regardless — the
        # authority is a remote party, not a trusted renderer.
        f"{html.escape(_s.programme)} · {html.escape(feature)}",
        h_sub,
    ))

    # ── Congratulatory citation ──────────────────────────────────────────
    total = int(summary.get("total") or len(cases) or 0)
    passed = int(summary.get("passed") or 0)
    exec_dt = _fmt_dt(executed_at)
    story.append(Paragraph("This is to certify that", body_c))
    citation = ParagraphStyle(
        "CitationName", parent=body, fontName="Helvetica-Bold",
        fontSize=16, leading=20, textColor=_accent(), alignment=1, spaceAfter=10,
    )
    story.append(Paragraph(html.escape(partner_name), citation))
    # "prescribed by <issuer>" only reads correctly when there IS an issuer; the
    # generic pack has none, so the clause is dropped rather than left dangling
    # as "prescribed by .".
    prescribed = (f" and has met the compliance standards prescribed by "
                  f"{html.escape(_s.issuer)}" if _s.issuer else "")
    story.append(Paragraph(
        f"has <b>successfully completed</b> the {html.escape(_s.programme)} programme for "
        f"<b>{html.escape(feature)}</b>, executed on <b>{html.escape(exec_dt)}</b>,"
        f"{prescribed}. All {total} test cases across "
        f"{html.escape(_s.authority_short)}-initiated and "
        f"{html.escape(_s.partner_short)}-initiated flows were executed and passed "
        f"within their expected response envelopes.",
        body,
    ))
    behalf = f"On behalf of {html.escape(_s.issuer)}, we" if _s.issuer else "We"
    story.append(Paragraph(
        f"{behalf} <b>congratulate</b> your team on this milestone "
        "and welcome you to the certified partner cohort. You are hereby "
        "authorised to proceed with the production rollout of this feature, "
        "subject to the standard change-window and monitoring obligations.",
        body,
    ))

    # ── Metadata block ──────────────────────────────────────────────────
    story.append(Paragraph("Certification particulars", h_section))
    td = test_data or {}
    meta_pairs = [
        ("Certified partner", partner_name),
        ("Feature", feature),
        ("Certification run ID", run_id),
        ("Executed at", exec_dt),
        ("Assigned role", _role_label(role)),
    ]
    # Identifier rows are whatever the run actually collected, labelled by the
    # pack. This was five hardcoded `if td.get("payer_vpa")`-style lines, which
    # meant a non-payments deployment could not surface its own identifiers at
    # all AND that a payments deployment printed "Payer VPA" onto every
    # document. `field_label` humanises anything the pack does not describe, so
    # an unknown key still appears rather than being dropped from a document
    # someone signs.
    pack = get_active_pack()
    for key, value in td.items():
        if value in (None, "", [], {}):
            continue
        meta_pairs.append((pack.field_label(str(key)), value))
    meta_pairs.append(("Total cases",   str(total)))
    meta_pairs.append(("Passed",        f"{passed} / {total}"))
    meta_pairs.append(("Result",        "PASS — All test cases within expected response envelope"))

    meta_tbl = Table(
        [[k, v] for k, v in meta_pairs],
        colWidths=[55 * mm, 115 * mm],
    )
    meta_tbl.setStyle(TableStyle([
        ("FONTNAME",   (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME",   (1, 0), (1, -1), "Helvetica"),
        ("FONTSIZE",   (0, 0), (-1, -1), 9.5),
        ("TEXTCOLOR",  (0, 0), (0, -1), _MUTED),
        ("TEXTCOLOR",  (1, 0), (1, -1), _NAVY),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("LINEBELOW",  (0, 0), (-1, -2), 0.4, _HAIR),
        ("VALIGN",     (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(meta_tbl)

    # ── Results table ───────────────────────────────────────────────────
    story.append(Paragraph("Test case results", h_section))
    header = ["#", "TC ID", "Description", "Scenario", "Init.", "Expected", "Actual", "Status"]
    tbl_data: list[list[Any]] = [header]
    body_cell = ParagraphStyle(
        "TCell", parent=body, fontSize=8.5, leading=11, spaceAfter=0,
        textColor=_INK,
    )
    mono_cell = ParagraphStyle(
        "TCMono", parent=body_cell, fontName="Courier-Bold",
        textColor=_NAVY,
    )
    def _esc(v: Any) -> str:
        return html.escape(str(v)) if v is not None else ""

    for i, c in enumerate(cases, start=1):
        tc_id   = _esc(c.get("test_case_id") or c.get("tc_id") or "")
        title   = _esc(c.get("title") or "—")
        scen    = _esc(_scenario_label(c.get("scenario") or "success"))
        # Wire token in, display label out. The token itself is pinned.
        #
        # An ABSENT initiator renders as "—", not as the authority. Defaulting
        # here attributed every case with a missing field to one side of the
        # exchange on a signed document — a factual claim about who ran the test,
        # invented from nothing. The old default was one domain's literal wire
        # token, so any other deployment printed its authority's short form for
        # rows it knew nothing about.
        raw_init = (c.get("initiated_by") or "").strip()
        init_by = _esc(_initiator_label(raw_init.upper())) if raw_init else "—"
        exp     = _esc(c.get("expected_code") or "—")
        act     = _esc(c.get("actual_code") or "—")
        status  = _esc(normalise(c.get("status")))
        tbl_data.append([
            Paragraph(str(i).zfill(2), body_cell),
            Paragraph(tc_id, mono_cell),
            Paragraph(title, body_cell),
            Paragraph(scen, body_cell),
            Paragraph(init_by, body_cell),
            Paragraph(exp, mono_cell),
            Paragraph(act, mono_cell),
            Paragraph(status, ParagraphStyle(
                "StatusCell", parent=body_cell, fontName="Helvetica-Bold",
                textColor=_status_colour(status),
            )),
        ])
    tbl = Table(
        tbl_data,
        # "Init." was 12mm and Description 51mm. 12mm fits about four
        # characters at 8.5pt, which was exactly enough for the two hardcoded
        # wire tokens NPCI and BANK — and nothing else. reportlab word-wraps
        # rather than truncates, so a pack whose labels are longer rendered
        # stacked fragments ("OMBUD" -> "OMB"/"UD") in a signed document.
        # 10mm moves from Description, which is the widest column and wraps
        # gracefully. Total stays at 170mm, the usable width inside A4 margins.
        colWidths=[8 * mm, 20 * mm, 41 * mm, 18 * mm, 22 * mm, 18 * mm, 18 * mm, 15 * mm],
        repeatRows=1,
    )
    tstyle = [
        ("BACKGROUND", (0, 0), (-1, 0), _STRIPE),
        ("TEXTCOLOR",  (0, 0), (-1, 0), _MUTED),
        ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",   (0, 0), (-1, 0), 8.5),
        ("ALIGN",      (0, 0), (0, -1), "CENTER"),
        ("ALIGN",      (4, 0), (4, -1), "CENTER"),
        ("ALIGN",      (7, 0), (7, -1), "CENTER"),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW",  (0, 0), (-1, 0), 0.6, _HAIR),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 5),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 5),
        ("BOX",        (0, 0), (-1, -1), 0.4, _HAIR),
        ("INNERGRID",  (0, 0), (-1, -1), 0.25, _HAIR),
    ]
    # Zebra striping — alternate rows for readability at print size.
    for r in range(1, len(tbl_data)):
        if r % 2 == 0:
            tstyle.append(("BACKGROUND", (0, r), (-1, r), _STRIPE))
    # Colour the initiator cell text per row.
    for r in range(1, len(tbl_data)):
        init = tbl_data[r][4].text if hasattr(tbl_data[r][4], "text") else ""
        # Compare against the DISPLAY label, because that is what the cell now
        # holds. Comparing to the raw "BANK" wire token here would silently
        # colour every partner-initiated row as authority-initiated under any
        # pack whose partner_short is not literally "BANK".
        col = _TEAL if init == _signoff().partner_short else _accent()
        tstyle.append(("TEXTCOLOR", (4, r), (4, r), col))
    tbl.setStyle(TableStyle(tstyle))
    story.append(tbl)
    story.append(Spacer(1, 14))

    # ── Attestation + signature block ───────────────────────────────────
    now = datetime.now(timezone.utc).astimezone()
    issued_on = now.strftime("%d %B %Y")
    # Place of issue comes from the pack, and is OMITTED when unset rather than
    # defaulted — the same rule the masthead applies to `issuer`. This was a
    # hardcoded city until 2026-09-07: every other identity on this document had
    # already moved to the pack, so a library deployment printed its own issuer
    # and signatory above a place of issue belonging to somebody else.
    issued_place = _signoff().issued_at
    attestation = (
        f"<b>Issued at:</b> {_esc(issued_place)} · <b>Date of issue:</b> {issued_on}"
        if issued_place else
        f"<b>Date of issue:</b> {issued_on}"
    )
    story.append(Paragraph(attestation, stamp))
    story.append(Spacer(1, 8))
    sig_tbl = Table([
        [
            Paragraph(
                "<font name='Helvetica-Bold' color='#0f172a' size='10'>Digitally signed for and on behalf of</font><br/>"
                f"<font color='#475569' size='9'>{html.escape(_s.signatory)}</font><br/>"
                "<br/>"
                # Was a second hardcoded "#1d4ed8" — the accent existed as a
                # constant and this line ignored it, so re-theming the document
                # left one stray line in the old brand colour.
                f"<font name='Helvetica-Oblique' size='11' color='{_s.accent}'>— Authorised Signatory —</font>",
                body,
            ),
            Paragraph(
                f"<font name='Helvetica-Bold' color='#0f172a' size='10'>Certificate reference</font><br/>"
                # run_id is partner-controlled (stored verbatim from cert_summary) and reaches
                # reportlab's Paragraph markup here — escape it like every sibling field, else an
                # injected <img src="http://…"> is fetched server-side by reportlab (SSRF).
                f"<font name='Courier' color='#0f172a' size='9.5'>{html.escape(run_id)}</font><br/>"
                f"<font color='#475569' size='9'>Verify at the A2A audit portal ›</font>",
                body,
            ),
        ],
    ], colWidths=[90 * mm, 80 * mm])
    sig_tbl.setStyle(TableStyle([
        ("LINEABOVE", (0, 0), (-1, 0), 0.6, _HAIR),
        ("TOPPADDING", (0, 0), (-1, 0), 12),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(sig_tbl)

    doc.build(story, onFirstPage=_draw_page_frame, onLaterPages=_draw_page_frame)
    return buf.getvalue()


def _role_label(role: str | None) -> str:
    """Display label for the certified role, from the active pack.

    Was a hardcoded four-entry map of UPI roles, which is also the reason a
    non-payments deployment had nothing valid to select upstream.
    """
    return get_active_pack().role_label(role)
