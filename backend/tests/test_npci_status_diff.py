"""Unit tests for the NPCI reconciliation diff (`app.api.dashboard.npci_status`).

Pure-function tests over `_local_view` + `_diff` — no DB, no network. The HTTP
handler is thin assembly over these two; the classification of "what counts as
a discrepancy, and what should the bank do about it" is the part worth pinning.

A false negative here is the dangerous direction: reporting "in sync" while the
two sides actually disagree is worse than no panel at all, because the partner
will trust it.
"""
from app.api.dashboard.npci_status import _STAGES, _diff, _local_view


class _Change:
    """Minimal stand-in for an IncomingChange row."""

    def __init__(self, **over):
        self.status = "received"
        self.decision = "pending"
        self.cert_status = "received"
        self.negotiation_version = 1
        self.negotiation_finalized_at = None
        self.__dict__.update(over)


def _npci(stage="communicated", *, done=None, steps=None, version=1,
          frozen=False, status="received", blocked=False, reason=None):
    done = done or {}
    return {
        "stage": {
            "current": stage,
            "steps": [{"key": k, "done": done.get(k, False)} for k in _STAGES],
        },
        "progress_steps": steps or {},
        "negotiation": {"version": version, "frozen": frozen},
        "assignment": {"status": status, "blocked": blocked, "blocked_reason": reason},
    }


def _fields(diffs):
    return {d["field"] for d in diffs}


class TestInSync:
    def test_matching_records_produce_no_discrepancies(self):
        change = _Change(decision="accepted")
        local = _local_view(change, set())
        npci = _npci("accepted", done={"communicated": True, "accepted": True},
                     status="accepted")
        assert _diff(local, npci) == []

    def test_every_entry_carries_both_values_and_a_hint(self):
        # The UI renders these verbatim — a discrepancy with no hint is a
        # dead end for the operator reading it.
        local = _local_view(_Change(decision="accepted", cert_status="tested"), set())
        diffs = _diff(local, _npci("communicated", version=3, status="received"))
        assert diffs
        for d in diffs:
            assert d["label"] and d["hint"]
            assert "local" in d and "npci" in d
            assert d["severity"] in ("mismatch", "info")


class TestMilestones:
    def test_we_reported_npci_did_not_record(self):
        local = _local_view(_Change(decision="accepted"), {"design_completed"})
        npci = _npci("accepted", done={"communicated": True, "accepted": True},
                     status="accepted")
        d = next(x for x in _diff(local, npci) if x["field"] == "design_completed")
        assert d["local"] == "reported"
        assert d["npci"] == "not recorded"
        assert "re-submit" in d["hint"].lower()

    def test_npci_recorded_and_we_did_not(self):
        # The opposite direction: our copy is the stale one, so telling the
        # partner to re-send would be exactly wrong advice.
        local = _local_view(_Change(decision="accepted"), set())
        npci = _npci("design",
                     done={"communicated": True, "accepted": True, "design": True},
                     steps={"design_completed": "2026-08-01T10:00:00+00:00"},
                     status="accepted")
        d = next(x for x in _diff(local, npci) if x["field"] == "design_completed")
        assert d["local"] == "not reported"
        assert "stale" in d["hint"].lower()

    def test_agreement_on_a_milestone_is_not_reported(self):
        local = _local_view(_Change(decision="accepted"), {"design_completed"})
        npci = _npci("design",
                     done={"communicated": True, "accepted": True, "design": True},
                     steps={"design_completed": "2026-08-01T10:00:00+00:00"},
                     status="accepted")
        assert "design_completed" not in _fields(_diff(local, npci))


class TestAcceptance:
    def test_we_accepted_npci_did_not(self):
        local = _local_view(_Change(decision="accepted"), set())
        npci = _npci("communicated", done={"communicated": True}, status="received")
        assert "accepted" in _fields(_diff(local, npci))

    def test_pending_on_both_sides_is_in_sync(self):
        local = _local_view(_Change(decision="pending"), set())
        npci = _npci("communicated", done={"communicated": True}, status="received")
        assert "accepted" not in _fields(_diff(local, npci))


class TestCertStatus:
    def test_assignment_status_maps_back_to_our_vocabulary(self):
        # NPCI stores APPLIED for what we call 'deployed'; a naive string
        # compare would flag every deployed change as a discrepancy.
        local = _local_view(_Change(decision="accepted", cert_status="deployed"), set())
        npci = _npci("accepted", done={"communicated": True, "accepted": True},
                     status="applied")
        assert "cert_status" not in _fields(_diff(local, npci))

    def test_genuine_cert_drift_is_reported(self):
        local = _local_view(_Change(decision="accepted", cert_status="tested"), set())
        npci = _npci("accepted", done={"communicated": True, "accepted": True},
                     status="applied")
        d = next(x for x in _diff(local, npci) if x["field"] == "cert_status")
        assert d["local"] == "tested" and d["npci"] == "deployed"

    def test_unknown_assignment_status_is_not_guessed(self):
        # An assignment status we have no mapping for (e.g. 'withdrawn') must
        # produce no cert claim rather than a fabricated one.
        local = _local_view(_Change(decision="accepted", cert_status="tested"), set())
        npci = _npci("communicated", done={"communicated": True}, status="withdrawn")
        assert "cert_status" not in _fields(_diff(local, npci))


class TestKitVersionAndFreeze:
    def test_npci_ahead_means_we_hold_a_superseded_kit(self):
        local = _local_view(_Change(), set())
        d = next(x for x in _diff(local, _npci(version=2)) if x["field"] == "negotiation_version")
        assert "superseded" in d["hint"]

    def test_we_ahead_gets_the_other_advice(self):
        local = _local_view(_Change(negotiation_version=3), set())
        d = next(x for x in _diff(local, _npci(version=2)) if x["field"] == "negotiation_version")
        assert "superseded" not in d["hint"]

    def test_freeze_disagreement_is_reported(self):
        local = _local_view(_Change(), set())
        d = next(x for x in _diff(local, _npci(frozen=True)) if x["field"] == "frozen")
        assert d["local"] == "open" and d["npci"] == "frozen"


class TestBlocked:
    def test_blocked_is_informational_not_a_mismatch(self):
        # We hold no local mirror of NPCI's blocked flag, so there is nothing
        # to disagree with — surfacing it as a mismatch would make `in_sync`
        # false forever on any blocked assignment.
        local = _local_view(_Change(), set())
        d = next(x for x in _diff(local, _npci(blocked=True, reason="awaiting fix"))
                 if x["field"] == "blocked")
        assert d["severity"] == "info"
        assert d["npci"] == "awaiting fix"


class TestStageContract:
    def test_stage_keys_match_npci(self):
        # Must stay in lockstep with `_STAGE_LABELS` in the NPCI backend's
        # api/partner_status.py and LIFECYCLE_STAGES in the partner frontend.
        assert _STAGES == [
            "communicated", "accepted", "design", "coding", "testing",
            "ready_for_cert", "certified",
        ]
