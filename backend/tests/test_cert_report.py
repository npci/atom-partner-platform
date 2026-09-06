# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""CERT-7, partner half: the report endpoint serves what THIS side knows."""
import json

import pytest
from fastapi import HTTPException
from types import SimpleNamespace

from app.api.dashboard.cert_fix import cert_report
from app.models import IncomingChange
from app.services.cert_remediation import open_round

USER = SimpleNamespace(username="op", id="u1")


def test_report_combines_status_history_and_fix_rounds(db_session):
    db_session.add(IncomingChange(
        id="chg-1", npci_change_id="npci-chg-1", title="T",
        cert_status="ready_for_certification",
        cert_status_history=json.dumps({"received": "2026-08-30T10:00:00+00:00"})))
    db_session.commit()
    open_round(db_session, change_id="chg-1", cflow_id="CF-1", case_id="TC1",
               verdict={"test_case_id": "TC1", "assertion_failures": []})

    out = cert_report("chg-1", user=USER, db=db_session)
    assert out["cert_status"] == "ready_for_certification"
    assert out["status_history"]["received"].startswith("2026-08-30")
    assert out["fix_rounds"][0]["verdict_case_ids"] == ["TC1"]


def test_report_404s_an_unknown_change(db_session):
    with pytest.raises(HTTPException) as exc:
        cert_report("nope", user=USER, db=db_session)
    assert exc.value.status_code == 404
