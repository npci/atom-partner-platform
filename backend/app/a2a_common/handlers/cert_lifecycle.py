# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Partner-side cert lifecycle handlers — the bank's half of the A2A cert conversation.

NPCI (the cert engine) drives the conversation; these produce the bank's replies:
  cert_config_request      -> cert_config_submission  (the bank's config)
  cert_setup_notification  -> cert_test_preparation   (per-case data + readiness)
  cert_verdict_notification-> cert_waiver_request      (on a waiver-eligible failure)
cert_case_result / cert_waiver_decision / cert_signoff_notification are acknowledged; their
persistence rides the existing cert_test_response handler.

These four are dispatched from `partner_executor.build_handler_registry()`. They were
unwired between 68cd0a44 and this session — see the regression note in that module.

CERT-4: the bank's REAL values now come from its own stores — `partner_settings`
key `cert_config` (JSON, merged one level deep over the demo profile below) and
the per-change `change_test_data` rows the Test Data screen writes. The demo
literals STAY as the seeded fallback: a fresh clone still runs the demo
end-to-end, and the reply summaries label which source answered.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# ── The bank's onboarding profile ────────────────────────────────────────────
# Shaped per A2A spec Appendix B `cert_config_submission`: nested bank_identity /
# network / security, plus roles and the subset the bank is asking to be tested on.
#
# THE FLAT KEYS BELOW THE NESTED ONES ARE DELIBERATE, NOT LEGACY. precert keys banks
# on a 3-letter `bank_code` and addresses every cert run to a `psp_org_id`; the spec's
# `bank_identity` has neither field, and its nearest equivalents (`participant_code`,
# `nbin`) are different things. NPCI's mapper (`cert_agent/setup.py`) reads the nested
# shape and lets the flat keys win where present, so both travel until that gap is
# reconciled spec-side.
_BANK_IDENTITY = {
    "bank_name": "My Bank",
    "org_id": "MYORG1",
    "nbin": None,
    "ifsc": "MYPS0000001",
    "iin": None,
    "participant_code": "OLV101",
    "handle": "@mypsp",
    "acquirer_id": None,
}

_NETWORK = {
    # Where precert sends the ReqPay — this is precert-bank-sim (:8443).
    "host": "127.0.0.1",
    "port": 8443,
    "base_url": None,
    "egress_cidrs": [],
    "mpls_circuit_id": None,
}

# Certificates are out of scope in this stack. The spec is explicit that bodies are
# NEVER inline — A2A carries only `cert_ref` + `fingerprint_sha256`, uploaded out of
# band via Cflow/PKI — so nulls here are the honest value, not a placeholder to fill.
# NPCI's provisioner writes an empty `tbl_cert_file.hsm_file` and the connector never
# reads it, so transactions are unaffected.
_SECURITY = {
    "tls_tier": "none",
    "ssl_signer": None,
    "ssl_client_cert": None,
    "hsm_certificate": None,
}

# The subset the bank is asking to certify against. Per spec this is the BANK's
# choice (`requested_subset`), not NPCI's — NPCI's default only applies when the
# bank names nothing or names something that does not exist. Changing this one
# value changes what gets certified.
_REQUESTED_SUBSET = "Subset-A2A-BIDI"

_ROLES = ["remitter", "beneficiary"]

# Per-case values the bank owns. `ready` is the spec's execution gate: on an
# NPCI-initiated case, true permits execution; on a bank-initiated one it is
# ignored. Flip one to false to hold that case back and watch it report as
# skipped rather than run.
_CASE_DATA = {
    "amount": "100.00", "mpin": "000012",
    "payerVpa": "tester@mypsp", "payeeVpa": "tester2@mypsp",
    "ready": True,
}


def _demo_config() -> dict:
    return {
        "renews_cflow_id": None,
        "bank_identity": dict(_BANK_IDENTITY),
        "network": dict(_NETWORK),
        "security": dict(_SECURITY),
        "roles": list(_ROLES),
        "supported_protocol_versions": ["UPI 2.x"],
        "supported_features": [],
        "requested_subset": _REQUESTED_SUBSET,
        "contacts": [],
        "preferred_window": None,
        # Extension keys — see the module note above.
        "bank_name": _BANK_IDENTITY["bank_name"],
        "bank_code": "MYB",
        "bank_org_id": _BANK_IDENTITY["org_id"],
        "bank_ifsc": _BANK_IDENTITY["ifsc"],
        "psp_name": "MyPSP",
        "psp_org_id": "OLV101",
        "psp_code": "MYB01",
        "handler": "mypsp",
        "mpinlength": "6",
        "bank_server_ip": _NETWORK["host"],
        "bank_server_port": str(_NETWORK["port"]),
    }


def _stored_cert_config(db) -> dict | None:
    """The operator's own config from `partner_settings` key `cert_config`.

    It lives in partner_settings — NOT partner_profiles, which holds the
    PARTNER.md capability document; the two were confused once. Returns None
    when unset. A malformed stored value returns None too, logged at ERROR
    NAMING THE CONSEQUENCE: the write path (settings PUT) rejects non-JSON
    with a 400, so anything malformed here bypassed it, and a silent fallback
    would certify a configured bank against demo values with nobody told.
    """
    from app.models import PartnerSetting

    row = db.get(PartnerSetting, "cert_config")
    if not row or not (row.value or "").strip():
        return None
    try:
        parsed = json.loads(row.value)
        if not isinstance(parsed, dict):
            raise ValueError(f"cert_config must be a JSON object, got {type(parsed).__name__}")
        return parsed
    except (ValueError, TypeError) as exc:
        logger.error(
            "cert_config in partner_settings is not valid JSON (%s) — "
            "FALLING BACK TO DEMO VALUES: this bank will certify against the "
            "demo profile until the stored value is fixed via PUT /api/settings",
            exc,
        )
        return None


def _merge_config(demo: dict, stored: dict) -> dict:
    """One level deep, stored wins: an operator correcting `network.host`
    need not restate `bank_identity`, and a partial config must not wipe the
    nested blocks it does not mention."""
    merged = dict(demo)
    for key, value in stored.items():
        if isinstance(value, dict) and isinstance(demo.get(key), dict):
            merged[key] = {**demo[key], **value}
        else:
            merged[key] = value
    return merged


def _local_change_id(db, body) -> str | None:
    """Map the envelope's `change_id` — the AUTHORITY's id — onto this
    platform's own `IncomingChange.id`.

    Every partner-side store is keyed on the LOCAL id: the Test Data screen's
    `PUT /changes/{change_id}/test-data/{tc_id}` resolves its path parameter
    with `db.get(IncomingChange, ...)`, and `cert_fix.py` reads fix rounds the
    same way. The A2A envelope carries NPCI's id instead, and the two are equal
    only by accident. Reading `change_test_data` with the envelope's id
    therefore matched nothing: the operator filled the screen in, the save
    succeeded, and the rig still received empty `case_data` — failing every
    case with "no row for this case" while the row sat right there under the
    other key. Every other handler in this package (change_communication,
    round_opened, cert_test_response, …) already resolves via
    `npci_change_id`; these three call sites were the exception.

    Returns the authority's id unchanged when no `IncomingChange` matches, so a
    cert conversation about a change this platform never received still reads
    consistently rather than silently addressing a different change. That case
    is logged: it means the cert flow outran `change_communication`.
    """
    from app.models import IncomingChange

    npci_change_id = getattr(body, "change_id", None)
    if not npci_change_id:
        return None
    row = (db.query(IncomingChange)
             .filter(IncomingChange.npci_change_id == npci_change_id)
             .first())
    if row:
        return row.id
    logger.warning(
        "cert lifecycle: no IncomingChange for npci_change_id=%s — falling back "
        "to the authority's id for local lookups", npci_change_id,
    )
    return npci_change_id


def handle_cert_config_request(body, db) -> dict:
    """Reply to CERT_CONFIG_REQUEST with the bank's certification config."""
    stored = _stored_cert_config(db)
    config = _merge_config(_demo_config(), stored) if stored else _demo_config()
    source = "operator-configured (merged over demo defaults)" if stored else "demo profile"
    network = config.get("network") or {}
    return {
        "task_type": "cert_config_submission", "status": "accepted",
        "summary": (
            f"Certification configuration submitted ({source}): PSP "
            f"{config.get('psp_org_id')}; switch endpoint "
            f"{network.get('host')}:{network.get('port')}; requesting "
            f"{config.get('requested_subset')}; certificates provided out-of-band."
        ),
        "config": config,
    }


def handle_cert_setup_notification(body, db) -> dict:
    """Reply to CERT_SETUP_NOTIFICATION with per-case data for the announced scope.

    CERT-4's test-data rule: when the change has SOME stored `change_test_data`
    rows, each case with a row gets it (`ready=true` unless the row says
    otherwise), and a case WITHOUT a row returns `ready=false` with a reason —
    the bank has started configuring, and silently filling the gaps with demo
    numbers would certify values nobody chose. Only a change with NO rows at
    all falls back to the demo values, labelled as such.
    """
    payload = getattr(body, "payload", None) or {}
    # The spec's case_list is authoritative; `cases` is the platform's flat alias.
    cases = [c.get("case_id") for c in (payload.get("case_list") or []) if c.get("case_id")]
    if not cases:
        cases = payload.get("cases", []) or []

    from app.models import ChangeTestData

    change_id = _local_change_id(db, body)
    rows = (db.query(ChangeTestData).filter(ChangeTestData.change_id == change_id).all()
            if change_id else [])
    by_tc = {r.tc_id: r for r in rows}

    if by_tc:
        case_data: dict[str, dict] = {}
        for tc in cases:
            row = by_tc.get(tc)
            if row:
                data = dict(row.test_data or {})
                data.setdefault("ready", True)
                case_data[tc] = data
            else:
                case_data[tc] = {
                    "ready": False,
                    "reason": "no test data configured for this case on the Test Data screen",
                }
        source_note = "from the bank's Test Data screen"
    else:
        case_data = {tc: dict(_CASE_DATA) for tc in cases}
        source_note = "DEMO defaults (no per-change test data configured)"

    ready = sum(1 for d in case_data.values() if d.get("ready"))
    return {
        "task_type": "cert_test_preparation", "status": "accepted",
        "summary": (
            f"Test data prepared for {len(cases)} test case(s) {source_note}. "
            f"{ready} declared ready to execute."
        ),
        # `case_data` is the spec key; `test_data` is kept as an alias so an NPCI
        # side that has not picked up the rename still receives the values.
        "case_data": case_data,
        "test_data": case_data,
    }


def handle_cert_execution_start(body, db) -> dict:
    """ITA I-6: the authority's START SIGNAL for the partner-initiated half.

    For each named case, fire the certification trigger (Stage 1: the
    user-supplied URL in `partner_settings`) with the case's own data and the
    callback alias. Scheduling, never inline: this handler answers a
    synchronous A2A call, and each trigger is an outbound HTTP POST — same
    judgement as the no-LLM-in-handler rule. The trigger only STARTS a case;
    results arrive later as the app's real traffic through the tunnel and are
    reported via cert_case_result (reporter=bank).

    An unconfigured trigger is an HONEST ack: dispatched=0 with the reason,
    never a pretend-start — the authority records those cases as not reported,
    which is the truth.
    """
    from app.models import ChangeTestData, PartnerSetting
    from ._background import _spawn

    payload = getattr(body, "payload", None) or {}
    case_ids = [str(c) for c in (payload.get("case_ids") or []) if c]
    cert_context = dict(payload.get("cert_context") or {})
    # Prefer the full simulator block's endpoint: it carries the `?pack=`
    # selector binding THIS round's contract. A bare alias would have the SUT
    # exercise whatever baseline an absent `?pack=` resolves against — every
    # label says round N, the grading says baseline (ITA §12.5's false pass).
    _sim = payload.get("simulator") if isinstance(payload.get("simulator"), dict) else {}
    reply_via = (str(_sim.get("endpoint") or "").strip()
                 or f"a2a://{payload.get('simulator_alias') or ''}".rstrip("/"))

    url_row = db.get(PartnerSetting, "cert_trigger_url")
    secret_row = db.get(PartnerSetting, "cert_trigger_secret")
    trigger_url = (url_row.value if url_row else "").strip()
    trigger_secret = (secret_row.value if secret_row else "").strip() or None

    if not trigger_url:
        logger.warning("cert_execution_start: no cert_trigger_url configured — "
                       "%d case(s) NOT dispatched", len(case_ids))
        return {
            "task_type": "cert_execution_ack", "status": "accepted",
            "dispatched": 0, "case_ids": [],
            "summary": (f"Execution start received for {len(case_ids)} case(s) "
                        "but no certification trigger is configured "
                        "(Settings → cert_trigger_url) — nothing dispatched."),
        }

    change_id = _local_change_id(db, body)
    rows = (db.query(ChangeTestData).filter(ChangeTestData.change_id == change_id).all()
            if change_id else [])
    data_by_case = {r.tc_id: dict(r.test_data or {}) for r in rows}

    # The rig reports outcomes back through this platform, which needs the
    # NPCI change id to address the cert_case_result — carry it in the
    # context rather than making the rig guess it. Deliberately the ENVELOPE's
    # id, not the local one resolved above: this value goes back out on the
    # wire, where only the authority's id means anything.
    npci_change_id = getattr(body, "change_id", None)
    if npci_change_id:
        cert_context.setdefault("npci_change_id", str(npci_change_id))

    from app.services.integration_testing.trigger import fire_trigger

    # _spawn passes positional args only through to_thread; a bound worker per
    # case carries the keyword arguments across the hop.
    def _worker(tc: str) -> None:
        fire_trigger(
            trigger_url, trigger_secret,
            test_case_id=tc,
            cert_context={**cert_context, "test_case_id": tc},
            case_data=data_by_case.get(tc),
            reply_via=reply_via,
        )

    for tc in case_ids:
        _spawn(_worker, tc)

    return {
        "task_type": "cert_execution_ack", "status": "accepted",
        "dispatched": len(case_ids), "case_ids": case_ids,
        "summary": (f"Execution start accepted: {len(case_ids)} "
                    f"partner-initiated case(s) triggered; results will follow "
                    f"as cert_case_result (reporter=bank)."),
    }


def handle_cert_case_result(body, db) -> dict:
    """Ack a case result. For bank-initiated cases (reporter=bank) the bank
    formally reports the outcome back to NPCI — the spec makes cert_case_result
    bidirectional (Direction: Either; the bank fires for initiator=bank cases).
    This reply renders as a bank -> NPCI message in the conversation."""
    p = getattr(body, "payload", None) or {}
    if p.get("reporter") == "bank":
        tc = p.get("test_case_id") or p.get("case_id", "")
        # `status` carries the spec vocabulary (passed/failed/error) since Phase 3.
        result = p.get("status")
        return {
            "task_type": "cert_case_result", "reporter": "bank", "status": "accepted",
            "test_case_id": tc, "case_id": tc, "result": result,
            "summary": f"Bank reports test case {tc}: {result}.",
        }
    return {"status": "accepted"}  # NPCI-initiated: silent ack, no reply bubble


def handle_cert_verdict_notification(body, db) -> dict:
    """Reply to CERT_VERDICT_NOTIFICATION.

    CERT-5: branch on the authority's `classification`. A `real_defect` — a
    field breaking its own registry constraint, carrying the whole
    `assertion_failures` list — opens/appends the change's fix round and
    schedules the fix worker; replying with a waiver request would ask to
    waive a genuine violation. Anything else keeps the existing
    waiver-request reply byte-for-byte.
    """
    p = getattr(body, "payload", None) or {}
    tc = p.get("test_case_id") or p.get("case_id", "")

    if (p.get("classification") or "").strip().lower() == "real_defect":
        from app.services.cert_remediation import open_round, run_fix_round
        from ._background import _spawn

        # Local id: `cert_fix.py` lists and approves rounds under the id its
        # own routes carry, which is `IncomingChange.id`. Opening the round
        # under the authority's id left the operator's Fix Rounds screen empty
        # for a round that existed — the same key mismatch as the test data.
        rnd = open_round(db, change_id=_local_change_id(db, body) or "",
                         cflow_id=p.get("cflow_id"), case_id=tc, verdict=p)
        # Schedule, never run inline: this handler answers a synchronous A2A
        # call — same judgement as the C-4 no-LLM-in-handler rule.
        _spawn(run_fix_round, rnd.id)
        n_failures = len(p.get("assertion_failures") or [])
        return {
            "task_type": "cert_defect_ack", "status": "accepted",
            "test_case_id": tc, "case_id": tc,
            "fix_round": rnd.round_number,
            "summary": (
                f"Defect recorded for test case {tc} "
                f"({n_failures} field-level failure(s)); fix round "
                f"{rnd.round_number} covers {len(rnd.verdict_case_ids)} case(s)."),
        }

    return {
        "task_type": "cert_waiver_request", "status": "accepted",
        "summary": f"Waiver requested for test case {tc}: functionality not applicable to our deployment.",
        "test_case_id": tc, "case_id": tc,
        # Spec `cert_waiver_request` splits these: `category` is the enum
        # (non_applicable | deferred | infeasible | policy), `reason` the prose.
        "category": "non_applicable",
        "reason": "Functionality is not applicable to this deployment.",
    }
