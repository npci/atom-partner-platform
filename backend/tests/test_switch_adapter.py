"""The switch-adapter seam: the core asks, the adapter answers.

The static adapter must reproduce what cert_lifecycle hardcoded before the seam
existed; a partner-supplied adapter must be reachable through `SWITCH_ADAPTER`
without the core knowing anything about it.
"""
from types import SimpleNamespace

import pytest

from app.adapters import BankConfig, CaseData, SwitchAdapter, get_switch_adapter


@pytest.fixture(autouse=True)
def _fresh_adapter(monkeypatch):
    monkeypatch.delenv("SWITCH_ADAPTER", raising=False)
    get_switch_adapter.cache_clear()
    yield
    get_switch_adapter.cache_clear()


def _body(payload):
    return SimpleNamespace(payload=payload, change_id="C-1")


class _FakeAdapter(SwitchAdapter):
    """What a partner would write for their own switch."""

    def bank_config(self, subset_hint):
        return BankConfig(
            config={"requested_subset": subset_hint or "Subset-X", "bank_org_id": "ACME"},
            psp_org_id="ACMEPSP", switch_endpoint="switch.acme.example:9443")

    def case_data(self, subset, case_ids, payload):
        return CaseData(
            case_data={c: {"amount": "5.00", "ready": c != "HOLD"} for c in case_ids},
            detail="; 1 held back", extras={"acme_diag": ["HOLD"]})

    def switch_base(self, product, org_id):
        return "https://switch.acme.example:9443" if (product, org_id) == ("upi", "ACMEPSP") else None

    def bank_org_id(self):
        return "ACME"


# ── loading ──────────────────────────────────────────────────────────────────

def test_default_is_the_static_adapter():
    from app.adapters.static import StaticSwitchAdapter
    assert isinstance(get_switch_adapter(), StaticSwitchAdapter)


def test_a_dotted_path_loads_the_partners_adapter(monkeypatch):
    monkeypatch.setenv("SWITCH_ADAPTER", f"{__name__}:_FakeAdapter")
    assert isinstance(get_switch_adapter(), _FakeAdapter)


def test_a_malformed_spec_fails_loudly(monkeypatch):
    monkeypatch.setenv("SWITCH_ADAPTER", "no_colon_here")
    with pytest.raises(ValueError, match="SWITCH_ADAPTER"):
        get_switch_adapter()


def test_a_class_that_is_not_an_adapter_is_refused(monkeypatch):
    monkeypatch.setenv("SWITCH_ADAPTER", "types:SimpleNamespace")
    with pytest.raises(TypeError, match="not a SwitchAdapter"):
        get_switch_adapter()


# ── the static adapter reproduces the pre-seam behaviour ─────────────────────

def test_static_config_carries_both_the_nested_and_the_flat_keys():
    cfg = get_switch_adapter().bank_config("")
    c = cfg.config
    # nested (spec) shape
    assert c["bank_identity"]["participant_code"] == "NEWPSP"
    assert c["network"] == {"host": "127.0.0.1", "port": 8443, "base_url": None,
                            "egress_cidrs": [], "mpls_circuit_id": None}
    # flat keys precert's mapper prefers — string port is the one that survives the wire
    assert c["psp_org_id"] == "NEWPSP" and c["bank_org_id"] == "NEWBNK"
    assert c["bank_server_ip"] == "127.0.0.1" and c["bank_server_port"] == "8443"
    assert (cfg.psp_org_id, cfg.switch_endpoint) == ("NEWPSP", "127.0.0.1:8443")


def test_static_config_is_a_fresh_copy_each_call():
    a = get_switch_adapter().bank_config("").config
    a["bank_identity"]["org_id"] = "MUTATED"
    assert get_switch_adapter().bank_config("").config["bank_identity"]["org_id"] == "NEWBNK"


def test_static_subset_is_the_banks_choice_and_the_hint_only_fills_a_gap(monkeypatch):
    from app.adapters import static
    assert get_switch_adapter().bank_config("Subset-HINT").config["requested_subset"] == static._REQUESTED_SUBSET
    monkeypatch.setattr(static, "_REQUESTED_SUBSET", "")
    assert get_switch_adapter().bank_config("Subset-HINT").config["requested_subset"] == "Subset-HINT"


def test_static_case_data_covers_every_case_when_api_sim_is_down(monkeypatch):
    from app.adapters import static
    monkeypatch.setattr(static, "_fetch_partner_testdata", lambda subset: {})
    out = get_switch_adapter().case_data("S", ["RE_01", "RE_02"], {})
    assert set(out.case_data) == {"RE_01", "RE_02"}
    assert out.case_data["RE_01"]["payerVpa"] != out.case_data["RE_02"]["payerVpa"]
    assert out.detail == " (0 sourced from api-sim per-case testdata, 2 generated per-case)"


def test_static_case_data_prefers_a_seeded_row(monkeypatch):
    from app.adapters import static
    seeded = {"amount": "7.00", "ready": True, "payerVpa": "seed@x"}
    monkeypatch.setattr(static, "_fetch_partner_testdata", lambda subset: {"RE_01": seeded})
    out = get_switch_adapter().case_data("S", ["RE_01", "RE_02"], {})
    assert out.case_data["RE_01"] == seeded
    assert out.case_data["RE_02"]["payerVpa"] == "customer02@partnerbank"
    assert out.detail == " (1 sourced from api-sim per-case testdata, 1 generated per-case)"


def test_static_switch_base_fronts_only_its_own_identity():
    a = get_switch_adapter()
    assert a.switch_base("upi", "NEWPSP") == "https://127.0.0.1:8443"
    assert a.switch_base("upi", "OTHER") is None       # refuse, never guess
    assert a.switch_base("bbps", "NEWPSP") is None
    assert a.bank_org_id() == "NEWBNK"


# ── the handlers go through the seam ─────────────────────────────────────────

def test_config_reply_is_built_from_whatever_adapter_is_configured(monkeypatch):
    from app.a2a_common.handlers import handle_cert_config_request as h
    monkeypatch.setenv("SWITCH_ADAPTER", f"{__name__}:_FakeAdapter")
    out = h(_body({"scope": {"subset_hint": "Subset-H"},
                   "target_identity": {"org_id": "Q", "kind": "core"}}), None)
    assert out["task_type"] == "cert_config_submission"
    assert out["config"] == {"requested_subset": "Subset-H", "bank_org_id": "ACME"}
    assert "PSP ACMEPSP" in out["summary"] and "switch.acme.example:9443" in out["summary"]
    assert out["identity"]["org_id"] == "ACMEPSP" and out["identity"]["kind"] == "core"


def test_setup_reply_uses_adapter_data_and_keeps_diagnostics_out_of_case_data(monkeypatch):
    from app.a2a_common.handlers import handle_cert_setup_notification as h
    monkeypatch.setenv("SWITCH_ADAPTER", f"{__name__}:_FakeAdapter")
    out = h(_body({"subset": "S", "case_list": [{"case_id": "A"}, {"case_id": "HOLD"}]}), None)
    assert out["case_data"] == out["test_data"] == {
        "A": {"amount": "5.00", "ready": True}, "HOLD": {"amount": "5.00", "ready": False}}
    assert out["acme_diag"] == ["HOLD"]                 # beside case_data, never inside it
    assert "acme_diag" not in out["case_data"]["A"]
    assert "1 declared ready to execute; 1 held back." in out["summary"]
    assert "2 test case(s)" in out["summary"]


def test_setup_falls_back_to_the_flat_cases_alias(monkeypatch):
    from app.a2a_common.handlers import handle_cert_setup_notification as h
    monkeypatch.setenv("SWITCH_ADAPTER", f"{__name__}:_FakeAdapter")
    out = h(_body({"cases": ["X", "Y"]}), None)
    assert set(out["case_data"]) == {"X", "Y"}


# ── optional hooks ───────────────────────────────────────────────────────────

def test_a_plain_adapter_inherits_do_nothing_hooks():
    import asyncio
    a = _FakeAdapter()
    reply = {"status": "accepted"}
    assert asyncio.run(a.on_reply("cert_config_request", {}, reply, change_id="C", cflow_id=None)) is reply
    assert asyncio.run(a.after_reply("cert_config_request", {}, reply, change_id="C", cflow_id=None)) is None
    assert a.api_router() is None


class _HookedAdapter(_FakeAdapter):
    calls: list = []

    async def on_reply(self, task_type, payload, reply, *, change_id, cflow_id):
        _HookedAdapter.calls.append(("on_reply", task_type, change_id, cflow_id))
        return {**reply, "edited": True}

    async def after_reply(self, task_type, payload, reply, *, change_id, cflow_id):
        _HookedAdapter.calls.append(("after_reply", task_type, reply.get("edited")))
        raise RuntimeError("a failing follow-up must not fail the delivered reply")


def test_executor_applies_on_reply_before_sending_and_survives_a_failing_after_reply(monkeypatch):
    import asyncio

    from app.a2a_common import partner_executor as pe

    monkeypatch.setenv("SWITCH_ADAPTER", f"{__name__}:_HookedAdapter")
    _HookedAdapter.calls = []
    monkeypatch.setattr(pe, "build_handler_registry",
                        lambda: {"cert_config_request": lambda body, db: {"status": "accepted"}})
    monkeypatch.setattr("app.database.SessionLocal", lambda: type("S", (), {"close": lambda s: None})())

    sent = []

    class Upd:
        async def start_work(self): pass
        async def add_artifact(self, parts, name, last_chunk):
            from app.a2a_common.partner_executor import _part_to_dict
            sent.append(_part_to_dict(parts[0]))
        async def complete(self): sent.append("complete")
        async def failed(self): sent.append("failed")

    monkeypatch.setattr(pe, "TaskUpdater", lambda *a, **k: Upd())
    monkeypatch.setattr(pe.PartnerAgentExecutor, "_extract_data", lambda self, ctx: {
        "task_type": "cert_config_request", "change_id": "CH-1", "cflow_id": "CF-1",
        "payload": {"x": 1}})
    ctx = type("Ctx", (), {"task_id": "t", "context_id": "c", "message": object()})()
    asyncio.run(pe.PartnerAgentExecutor().execute(ctx, object()))

    assert sent == [{"status": "accepted", "edited": True}, "complete"]   # edited reply, no "failed"
    assert _HookedAdapter.calls == [("on_reply", "cert_config_request", "CH-1", "CF-1"),
                                    ("after_reply", "cert_config_request", True)]
