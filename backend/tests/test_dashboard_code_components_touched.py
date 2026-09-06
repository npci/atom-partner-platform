# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for _latest_design_components_touched (SDLC Gap 2 wiring helper)."""
import json

from app.api.dashboard.code import _latest_design_components_touched
from app.models import DesignReport


def _add_design(db_session, change_id, components, version=1):
    db_session.add(DesignReport(
        change_id=change_id, version=version,
        content=json.dumps({"components_touched": components}),
    ))
    db_session.commit()


def test_returns_empty_list_with_no_design_report(db_session):
    assert _latest_design_components_touched(db_session, "no-such-change") == []


def test_returns_components_from_latest_design(db_session):
    _add_design(db_session, "c1", ["PaymentRouter", "SettlementService"])
    assert _latest_design_components_touched(db_session, "c1") == ["PaymentRouter", "SettlementService"]


def test_returns_highest_version(db_session):
    _add_design(db_session, "c1", ["v1_component"], version=1)
    _add_design(db_session, "c1", ["v2_component"], version=2)
    assert _latest_design_components_touched(db_session, "c1") == ["v2_component"]


def test_tolerant_of_malformed_json(db_session):
    db_session.add(DesignReport(change_id="c1", version=1, content="not json"))
    db_session.commit()
    assert _latest_design_components_touched(db_session, "c1") == []


def test_tolerant_of_missing_field(db_session):
    db_session.add(DesignReport(change_id="c1", version=1, content=json.dumps({"other": "x"})))
    db_session.commit()
    assert _latest_design_components_touched(db_session, "c1") == []


def test_filters_non_string_entries(db_session):
    _add_design(db_session, "c1", ["Valid", 123, None, "  ", "AlsoValid"])
    assert _latest_design_components_touched(db_session, "c1") == ["Valid", "AlsoValid"]
