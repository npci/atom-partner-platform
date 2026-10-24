"""Txn-over-A2A wrapper tests (docs/upi_over_a2a_plan.md, M3 3c).

Mirrors backend/tests/a2a_common/test_txn_wrappers.py on the bank side: refusals as
named faults, dedup never re-firing the switch, run-window gating off the cert
conversation, both byte-fidelity halves (Ingress → switch bytes identical; Egress →
ACK bytes back identical), and the executor's interception of txn_* ahead of the
handler registry.
"""
from __future__ import annotations

import asyncio
import base64

import pytest

from app.a2a_common import txn_egress as eg
from app.a2a_common import txn_ingress as ig
from app.a2a_common import txn_state
from app.adapters import BankConfig, CaseData, SwitchAdapter, get_switch_adapter

ORG = "OLV101"
REQ_XML = '<upi:ReqMandate>payer=वि</upi:ReqMandate>'.encode()
ACK_XML = b'<Ack api="ReqPay" err=""/>'
URI = "/upi/ReqPay/2.0/urn:txnid:NPCIabc123"


class _Adapter(SwitchAdapter):
    """Fronts exactly one identity, like the stock adapter does."""

    def bank_config(self, subset_hint):
        return BankConfig(config={"requested_subset": ""}, psp_org_id=ORG,
                          switch_endpoint="127.0.0.1:8443")

    def case_data(self, subset, case_ids, payload):
        return CaseData(case_data={})

    def switch_base(self, product, org_id):
        return "https://127.0.0.1:8443" if (product, org_id) == ("upi", ORG) else None

    def bank_org_id(self):
        return "BANK1"


@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "txn_hop_log_path", str(tmp_path / "hops.jsonl"))
    monkeypatch.setenv("SWITCH_ADAPTER", f"{__name__}:_Adapter")
    get_switch_adapter.cache_clear()
    txn_state._reset()
    yield
    txn_state._reset()
    get_switch_adapter.cache_clear()


def _inbound(payload_overrides=None, message_id="MID-1", **env_overrides):
    data = {
        "protocol_version": "1.0", "message_id": message_id,
        "task_type": "txn_request", "from": "npci-platform",
        "cflow_id": "CFLOW-TEST", "correlation_id": "CORR-1",
        "payload": {
            "initiated_by": "NPCI",
            "certification": {"product": "upi", "org_id": ORG},
            "txn": {
                "header": {"content_type": "application/xml; charset=UTF-8"},
                "metadata": {"product": "upi", "uri": URI,
                             "txn_ref": "NPCIabc123", "size_bytes": len(REQ_XML)},
                "txn_payload": base64.b64encode(REQ_XML).decode(),
            },
        },
    }
    data.update(env_overrides)
    if payload_overrides:
        data["payload"].update(payload_overrides)
    return data


class _FakeResp:
    def __init__(self, content=ACK_XML, status=200):
        self.content = content
        self.status_code = status
        self.headers = {"content-type": "application/xml"}


class _FakeSwitch:
    received: list = []

    def __init__(self, *a, **k): ...
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def post(self, url, content=b"", headers=None):
        _FakeSwitch.received.append((url, content, headers))
        return _FakeResp()


# ── Ingress ──────────────────────────────────────────────────────────────────

def test_ingress_relays_bytes_byte_identically(monkeypatch):
    import httpx
    _FakeSwitch.received = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeSwitch)

    reply = asyncio.run(ig.process_inbound_txn(_inbound()))

    url, received, headers = _FakeSwitch.received[0]
    assert received == REQ_XML, "switch must get the exact signed bytes"
    assert url == f"https://127.0.0.1:8443{URI}", "uri joined to OUR switch base"
    assert base64.b64decode(reply["txn"]["txn_payload"]) == ACK_XML
    assert reply["txn"]["metadata"]["http_status"] == 200
    # The only visibility NPCI gets into our internal legs: reported timings.
    assert "received_at" in reply["txn"]["metadata"]
    assert "latency_ms" in reply["txn"]["metadata"]


def test_ingress_wrong_direction():
    data = _inbound({"initiated_by": "BANK"})   # bank→NPCI combo arriving AT the bank
    reply = asyncio.run(ig.process_inbound_txn(data))
    assert reply["fault"]["fault_code"] == "WRONG_DIRECTION"


def test_ingress_unknown_identity_refused_never_guessed():
    data = _inbound()
    data["payload"]["certification"]["org_id"] = "SBIP002"
    reply = asyncio.run(ig.process_inbound_txn(data))
    assert reply["fault"]["fault_code"] == "UNKNOWN_PARTNER"


def test_ingress_unknown_target():
    data = _inbound()
    data["payload"]["txn"]["metadata"]["uri"] = "/upi/ReqPay/2.0/../../etc"
    reply = asyncio.run(ig.process_inbound_txn(data))
    assert reply["fault"]["fault_code"] == "UNKNOWN_TARGET"


def test_ingress_dedup_replays_without_refiring(monkeypatch):
    import httpx
    _FakeSwitch.received = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeSwitch)
    asyncio.run(ig.process_inbound_txn(_inbound()))
    assert len(_FakeSwitch.received) == 1
    second = asyncio.run(ig.process_inbound_txn(_inbound()))
    assert len(_FakeSwitch.received) == 1, "duplicate must NOT re-fire the switch"
    assert base64.b64decode(second["txn"]["txn_payload"]) == ACK_XML


def test_ingress_switch_down_is_a_named_fault(monkeypatch):
    import httpx

    class _Down(_FakeSwitch):
        async def post(self, *a, **k):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "AsyncClient", _Down)
    reply = asyncio.run(ig.process_inbound_txn(_inbound()))
    assert reply.get("txn") is None
    assert reply["fault"]["fault_code"] == "SWITCH_UNREACHABLE"


def test_ingress_routes_through_whatever_switch_the_adapter_names(monkeypatch):
    """The core never decides where a switch lives — the adapter does."""
    import httpx
    _FakeSwitch.received = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeSwitch)
    monkeypatch.setattr(_Adapter, "switch_base",
                        lambda self, p, o: "https://10.0.0.5:9443/")   # trailing slash tolerated
    asyncio.run(ig.process_inbound_txn(_inbound()))
    assert _FakeSwitch.received[0][0] == f"https://10.0.0.5:9443{URI}"


# ── Run window off the conversation ──────────────────────────────────────────

def test_observe_conversation_opens_and_closes_the_window():
    assert txn_state.cflow_for("upi", ORG) is None
    txn_state.observe_conversation("cert_setup_notification", "CFLOW-9")
    assert txn_state.cflow_for("upi", ORG) == "CFLOW-9"
    txn_state.observe_conversation("cert_signoff_notification", None)
    assert txn_state.cflow_for("upi", ORG) is None


def test_window_is_keyed_by_the_identity_npci_names():
    txn_state.observe_conversation("cert_setup_notification", "CFLOW-A",
                                   identity={"product": "upi", "org_id": "OTHER"})
    assert txn_state.cflow_for("upi", "OTHER") == "CFLOW-A"
    assert txn_state.cflow_for("upi", ORG) is None


# ── Egress ───────────────────────────────────────────────────────────────────

def test_egress_round_trips_the_ack_byte_identically(monkeypatch):
    txn_state.observe_conversation("cert_setup_notification", "CFLOW-9")
    sent = {}

    async def fake_send(cflow_id, message_id, envelope):
        sent.update(cflow=cflow_id, env=envelope)
        assert base64.b64decode(envelope["payload"]["txn"]["txn_payload"]) == REQ_XML
        return {"txn": {"header": {"content_type": "application/xml"},
                        "metadata": {"product": "upi", "txn_ref": "NPCIabc123",
                                     "size_bytes": len(ACK_XML), "http_status": 200},
                        "txn_payload": base64.b64encode(ACK_XML).decode()}}

    monkeypatch.setattr(eg, "_send", fake_send)
    resp = asyncio.run(eg._relay("upi", ORG, URI, REQ_XML, "application/xml"))
    assert resp.status_code == 200
    assert resp.body == ACK_XML, "switch must get the exact ACK bytes"
    assert sent["cflow"] == "CFLOW-9"
    assert sent["env"]["task_type"] == "txn_request"
    assert sent["env"]["payload"]["initiated_by"] == "BANK"
    assert sent["env"]["from"] == f"bank-{ORG}"


def test_egress_answers_npci_initiated_as_txn_response(monkeypatch):
    txn_state.observe_conversation("cert_setup_notification", "CFLOW-9")
    txn_state.note_inbound_request("NPCIabc123")   # NPCI's request came through Ingress
    sent = {}

    async def fake_send(cflow_id, message_id, envelope):
        sent.update(env=envelope)
        return {"txn": {"header": {"content_type": "application/xml"},
                        "metadata": {"size_bytes": len(ACK_XML)},
                        "txn_payload": base64.b64encode(ACK_XML).decode()}}

    monkeypatch.setattr(eg, "_send", fake_send)
    resp = asyncio.run(eg._relay("upi", ORG, "/upi/RespPay/2.0/urn:txnid:NPCIabc123",
                                 REQ_XML, "application/xml"))
    assert resp.status_code == 200
    assert sent["env"]["task_type"] == "txn_response"
    assert sent["env"]["payload"]["initiated_by"] == "NPCI"


def test_egress_refuses_outside_the_run_window(monkeypatch):
    async def fake_send(*a):   # must never be reached
        raise AssertionError("refused traffic must not be routed")

    monkeypatch.setattr(eg, "_send", fake_send)
    assert asyncio.run(eg._relay("upi", ORG, URI, REQ_XML, "application/xml")).status_code == 502


def test_egress_refuses_foreign_identity_and_bad_uri(monkeypatch):
    txn_state.observe_conversation("cert_setup_notification", "CFLOW-9")

    async def fake_send(*a):
        raise AssertionError("refused traffic must not be routed")

    monkeypatch.setattr(eg, "_send", fake_send)
    assert asyncio.run(eg._relay("upi", "SBIP002", URI, REQ_XML, "application/xml")).status_code == 502
    assert asyncio.run(eg._relay("upi", ORG, "/nope", REQ_XML, "application/xml")).status_code == 502


def test_egress_fault_gives_the_switch_no_ack(monkeypatch):
    txn_state.observe_conversation("cert_setup_notification", "CFLOW-9")

    async def fake_send(*a):
        return None   # A2A delivery failed

    monkeypatch.setattr(eg, "_send", fake_send)
    resp = asyncio.run(eg._relay("upi", ORG, URI, REQ_XML, "application/xml"))
    assert resp.status_code == 502
    assert not resp.body, "never a fabricated ACK or NACK"


def test_egress_send_authenticates_as_the_adapters_bank_in_a_thread(monkeypatch):
    """The partner's auth is blocking + DB-backed; `_send` must hand it the adapter's
    bank org id (to pick the right api key) and not run it on the event loop."""
    seen = {}

    def fake_ctx(bank_org_id):
        import threading
        seen["bank"] = bank_org_id
        seen["thread"] = threading.current_thread() is not threading.main_thread()
        return ("tok", "http://npci.test", "hmac-secret")

    async def fake_a2a(**kw):
        seen["kw"] = kw
        return {"txn": {}}

    monkeypatch.setattr(eg, "_auth_context", fake_ctx)
    import app.a2a_common.client as client
    monkeypatch.setattr(client, "send_a2a_message", fake_a2a)

    reply = asyncio.run(eg._send("CFLOW-9", "MID", {"x": 1}))

    assert reply == {"txn": {}}
    assert seen["bank"] == "BANK1" and seen["thread"] is True
    assert seen["kw"]["auth_header"] == "Bearer tok" and seen["kw"]["hmac_secret"] == "hmac-secret"
    assert seen["kw"]["context_id"] == "CFLOW-9" and seen["kw"]["base_url"] == "http://npci.test"


def test_egress_send_with_no_token_is_a_fault_not_an_exception(monkeypatch):
    monkeypatch.setattr(eg, "_auth_context", lambda bank: None)
    assert asyncio.run(eg._send("C", "M", {})) is None


# ── Executor wiring ──────────────────────────────────────────────────────────

class _Updater:
    def __init__(self):
        self.events = []

    async def add_artifact(self, parts, name, last_chunk):
        self.events.append(("artifact", name))

    async def complete(self):
        self.events.append(("complete",))

    async def failed(self):
        self.events.append(("failed",))

    async def start_work(self):
        pass


def _run_executor(monkeypatch, data):
    from app.a2a_common import partner_executor as pe

    upd = _Updater()
    monkeypatch.setattr(pe, "TaskUpdater", lambda *a, **k: upd)
    monkeypatch.setattr(pe.PartnerAgentExecutor, "_extract_data", lambda self, ctx: data)
    ctx = type("Ctx", (), {"task_id": "t", "context_id": "c", "message": object()})()
    asyncio.run(pe.PartnerAgentExecutor().execute(ctx, object()))
    return upd


def test_executor_routes_txn_to_ingress_before_the_handler_registry(monkeypatch):
    import httpx
    _FakeSwitch.received = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeSwitch)
    upd = _run_executor(monkeypatch, _inbound())
    assert upd.events == [("artifact", "txn-reply"), ("complete",)]
    assert len(_FakeSwitch.received) == 1


def test_executor_fails_a_txn_with_no_message_id(monkeypatch):
    data = _inbound()
    data.pop("message_id")
    assert _run_executor(monkeypatch, data).events[-1] == ("failed",)


def test_executor_opens_the_run_window_from_the_cert_conversation(monkeypatch):
    from app.a2a_common import partner_executor as pe
    monkeypatch.setattr(pe, "build_handler_registry",
                        lambda: {"cert_setup_notification": lambda body, db: {"status": "accepted"}})
    monkeypatch.setattr("app.database.SessionLocal", lambda: type("S", (), {"close": lambda s: None})())
    upd = _run_executor(monkeypatch, {
        "task_type": "cert_setup_notification", "cflow_id": "CFLOW-77",
        "payload": {"subset": "S", "cases": ["RE_01"]}})
    assert upd.events[-1] == ("complete",)
    assert txn_state.cflow_for("upi", ORG) == "CFLOW-77"


def test_the_refuse_loudly_stubs_never_fabricate_an_ack():
    from app.a2a_common.handlers import handle_txn_request, handle_txn_response
    for h in (handle_txn_request, handle_txn_response):
        out = h(None, None)
        assert out["fault"]["fault_code"] == "NOT_ENABLED" and "txn" not in out


# ── The real listener, started the way app.main starts it ───────────────────

def _self_signed(tmp_path):
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    crt, k = tmp_path / "w.crt", tmp_path / "w.key"
    crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    k.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                    serialization.PrivateFormat.TraditionalOpenSSL,
                                    serialization.NoEncryption()))
    return str(crt), str(k)


def _free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_main_starts_a_tls_egress_that_routes_a_switch_post_to_npci(tmp_path, monkeypatch):
    import httpx

    from app import main
    from app.config import settings

    crt, key = _self_signed(tmp_path)
    port = _free_port()
    monkeypatch.setattr(settings, "txn_egress_autostart", True)
    monkeypatch.setattr(settings, "txn_egress_port", port)
    monkeypatch.setattr(settings, "txn_egress_ssl_cert", crt)
    monkeypatch.setattr(settings, "txn_egress_ssl_key", key)

    sent = {}

    async def fake_send(cflow_id, message_id, envelope):
        sent["env"] = envelope
        return {"txn": {"header": {"content_type": "application/xml"},
                        "metadata": {"size_bytes": len(ACK_XML), "http_status": 200},
                        "txn_payload": base64.b64encode(ACK_XML).decode()}}

    monkeypatch.setattr(eg, "_send", fake_send)
    txn_state.observe_conversation("cert_setup_notification", "CFLOW-LIVE")

    async def scenario():
        await main._start_txn_egress()
        async with httpx.AsyncClient(verify=False, timeout=5) as c:
            for _ in range(50):                        # wait for the listener
                try:
                    if (await c.get(f"https://127.0.0.1:{port}/healthz")).status_code == 200:
                        break
                except httpx.TransportError:
                    await asyncio.sleep(0.1)
            r = await c.post(f"https://127.0.0.1:{port}/t/upi/{ORG}{URI}", content=REQ_XML,
                             headers={"content-type": "application/xml"})
            refused = await c.post(f"https://127.0.0.1:{port}/t/upi/SBIP002{URI}", content=REQ_XML)
        return r, refused

    r, refused = asyncio.run(scenario())
    assert r.status_code == 200 and r.content == ACK_XML
    assert sent["env"]["payload"]["certification"] == {"product": "upi", "org_id": ORG}
    assert sent["env"]["payload"]["txn"]["metadata"]["uri"] == URI
    assert refused.status_code == 502


def test_main_skips_the_egress_when_the_tls_pair_is_missing(tmp_path, monkeypatch, caplog):
    from app import main
    from app.config import settings

    monkeypatch.setattr(settings, "txn_egress_autostart", True)
    monkeypatch.setattr(settings, "txn_egress_ssl_cert", str(tmp_path / "nope.crt"))
    monkeypatch.setattr(settings, "txn_egress_ssl_key", str(tmp_path / "nope.key"))
    with caplog.at_level("WARNING"):
        asyncio.run(main._start_txn_egress())
    assert "txn egress not started" in caplog.text
