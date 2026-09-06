# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the DB-first / file-fallback partner profile loader.

The profile is now UI-configurable: `agents/_common.read_partner_profile()`
prefers the active `partner_profiles` row and falls back to the mounted file at
`settings.partner_profile_path` when no row exists.
"""
from app.agents import _common
from app.config import settings
from app.models import PartnerProfile

FILE_PROFILE = "---\npartner: File Bank\nprofile_version: 1.0\n---\n# File Bank\n- caps: file\n"
DB_PROFILE = "---\npartner: DB Bank\nprofile_version: 2.5\n---\n# DB Bank\n- caps: db\n"


def test_falls_back_to_file_when_no_db_row(db_session, tmp_path, monkeypatch):
    f = tmp_path / "partner_profile.md"
    f.write_text(FILE_PROFILE, encoding="utf-8")
    monkeypatch.setattr(settings, "partner_profile_path", str(f))

    raw, fm = _common.read_partner_profile()

    assert "File Bank" in raw
    assert fm["partner"] == "File Bank"
    assert fm["profile_version"] == "1.0"


def test_prefers_db_row_over_file(db_session, tmp_path, monkeypatch):
    f = tmp_path / "partner_profile.md"
    f.write_text(FILE_PROFILE, encoding="utf-8")
    monkeypatch.setattr(settings, "partner_profile_path", str(f))

    db_session.add(PartnerProfile(
        partner_name="DB Bank", profile_version="2.5", content=DB_PROFILE, source="edit",
    ))
    db_session.commit()

    raw, fm = _common.read_partner_profile()

    assert "DB Bank" in raw
    assert "File Bank" not in raw
    assert fm["partner"] == "DB Bank"
    assert fm["profile_version"] == "2.5"


def test_empty_db_content_falls_back_to_file(db_session, tmp_path, monkeypatch):
    """A blank/whitespace-only row must not shadow the file fallback."""
    f = tmp_path / "partner_profile.md"
    f.write_text(FILE_PROFILE, encoding="utf-8")
    monkeypatch.setattr(settings, "partner_profile_path", str(f))

    db_session.add(PartnerProfile(content="   \n", source="edit"))
    db_session.commit()

    raw, _ = _common.read_partner_profile()

    assert "File Bank" in raw
