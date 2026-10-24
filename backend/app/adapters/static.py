"""Stock adapter: a configured demo profile, like a real bank's onboarding profile.

This is what `cert_lifecycle.py` hardcoded before the adapter seam existed, moved
here unchanged. It maps to precert PSP OLV101 / precert-bank-sim (:8443).

Per-case data is looked up on api-sim's ``GET /internal/subset/{subset}/testdata``
— seeded per feature via ``scripts/install_partner_testdata.sh`` and also what the
api-sim RunSubset console pre-fills, so what the bank *declares* in the A2A reply
matches what the operator *fires* end-to-end. If api-sim is unreachable, or has no
row for a case, a per-case generated stub fills in so the reply always travels.

The api-sim lookup is internal-test plumbing, not something a partner has; a
partner replaces this whole adapter.
"""
from __future__ import annotations

import json
import logging
import os
import re
import ssl
import urllib.request
from typing import Optional

from .base import BankConfig, CaseData, SwitchAdapter

logger = logging.getLogger(__name__)

# Where to reach api-sim from inside the partner-platform container. On dev
# compose this is host.docker.internal:8094 (api-sim runs on the host per §4.9);
# override for on-prem via the PARTNER_APISIM_URL env.
_APISIM_URL = os.environ.get("PARTNER_APISIM_URL", "https://host.docker.internal:8094")

# ── The bank's onboarding profile ────────────────────────────────────────────
# Shaped per A2A spec Appendix B `cert_config_submission`: nested bank_identity /
# network / security, plus roles and the subset the bank is asking to be tested on.
#
# THE FLAT KEYS IN `bank_config()` BELOW THE NESTED ONES ARE DELIBERATE, NOT LEGACY.
# precert keys banks on a 3-letter `bank_code` and addresses every cert run to a
# `psp_org_id`; the spec's `bank_identity` has neither field, and its nearest
# equivalents (`participant_code`, `nbin`) are different things. NPCI's mapper
# (`cert_agent/setup.py`) reads the nested shape and lets the flat keys win where
# present, so both travel until that gap is reconciled spec-side.
_BANK_IDENTITY = {
    "bank_name": "New Bank Ltd",
    "org_id": "NEWBNK",
    "nbin": None,
    "ifsc": "NEWB0000001",
    "iin": os.environ.get("PARTNER_BANK_IIN") or None,
    "participant_code": "NEWPSP",
    "handle": "@newpsp",
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
_REQUESTED_SUBSET = os.environ.get("PARTNER_REQUESTED_SUBSET", "Subset-A2A-BIDI")

_ROLES = [r.strip() for r in os.environ.get("PARTNER_ROLES", "remitter,beneficiary").split(",") if r.strip()]

# Fallback per-case values used only when api-sim is unreachable OR the case
# has no partner-declared row on the /internal/subset/.../testdata endpoint.
# `ready` is the spec's execution gate: on an NPCI-initiated case, true
# permits execution; on a bank-initiated one it is ignored. Flip one to false
# to hold that case back and watch it report as skipped rather than run.
_CASE_DATA_STUB = {
    "amount": "100.00", "mpin": "000012",
    "payerVpa": "tester@mypsp", "payeeVpa": "tester2@mypsp",
    "ready": True,
}


# UPI-standard bank IFSC used across all generated testdata. Constant because
# the seed banks (see install_partner_testdata.sh) all use the same IFSC and
# precert's pattern check just wants `[A-Z]{4}0…` shaped IFSCs — the actual
# routing is api-sim's job, not the payload's.
_STUB_IFSC = "PBIN0000001"


def _stub_for_case(case_id: str) -> dict:
    """Case-specific fallback used when api-sim has no seeded row for this
    case_id. Derives unique per-case identifiers from the numeric suffix so
    a fresh run (no seed) still gets meaningful, distinct values instead of
    every case reporting the same tester@mypsp / tester2@mypsp pair.

    Fields:
      payerVpa   customer<NN>@partnerbank
      beneVpa    merchant<NN>@partnerbank
      mobile     9999900<NN>  (10-digit, distinct per case)
      ac         1234500<NN>  (11-digit)
      ifsc       PBIN0000001  (constant — see _STUB_IFSC)
      amount, mpin, ready — invariant across cases (from _CASE_DATA_STUB)
    """
    m = re.search(r"\d+", case_id or "")
    n = int(m.group(0)) if m else 0
    nn2 = f"{n:02d}"
    # precert validates names as alphabetic only, so the digits become letters.
    tag = "".join("abcdefghij"[int(d)] for d in nn2).capitalize()
    return {
        "amount":   _CASE_DATA_STUB["amount"],
        "mpin":     _CASE_DATA_STUB["mpin"],
        "ready":    True,
        "payerVpa": f"customer{nn2}@partnerbank",
        "beneVpa":  f"merchant{nn2}@partnerbank",
        "payerName": f"Customer {tag}",
        "beneName":  f"Merchant {tag}",
        "mobile":   f"9999900{nn2}",
        "ac":       f"1234500{nn2}",
        "ifsc":     _STUB_IFSC,
    }


def _fetch_partner_testdata(subset: str) -> dict:
    """GET api-sim's per-case testdata for the given subset.

    Returns an empty dict on any failure — the caller falls back to
    ``_stub_for_case`` per case, so the reply always travels.
    """
    if not subset:
        return {}
    url = f"{_APISIM_URL.rstrip('/')}/internal/subset/{subset}/testdata"
    try:
        # api-sim on dev/prod is HTTPS with a self-signed cert (§4.9). The
        # endpoint carries no auth and only serves per-case JSON — verify
        # is off intentionally.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(url, context=ctx, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, dict):
            return data
        logger.warning("api-sim /testdata returned non-dict for %s: %r", subset, type(data))
    except Exception as e:
        logger.info("api-sim /testdata fetch failed for %s (%s) — falling back to stub", subset, e)
    return {}


class StaticSwitchAdapter(SwitchAdapter):
    def bank_config(self, subset_hint: str) -> BankConfig:
        subset = _REQUESTED_SUBSET or subset_hint or ""
        config = {
            "renews_cflow_id": None,
            "bank_identity": dict(_BANK_IDENTITY),
            "network": dict(_NETWORK),
            "security": dict(_SECURITY),
            "roles": list(_ROLES),
            "supported_protocol_versions": ["UPI 2.x"],
            "supported_features": [],
            "requested_subset": subset,
            "contacts": [],
            "preferred_window": None,
            # Extension keys — see the note above `_BANK_IDENTITY`.
            "bank_name": _BANK_IDENTITY["bank_name"],
            "bank_code": "NBK",
            "bank_org_id": _BANK_IDENTITY["org_id"],
            "bank_ifsc": _BANK_IDENTITY["ifsc"],
            "psp_name": "NewPSP",
            "psp_org_id": _BANK_IDENTITY["participant_code"],
            "psp_code": "NBK",
            "handler": "newpsp",
            "mpinlength": "6",
            "bank_server_ip": _NETWORK["host"],
            "bank_server_port": str(_NETWORK["port"]),
        }
        return BankConfig(
            config=config,
            psp_org_id=_BANK_IDENTITY["participant_code"],
            switch_endpoint=f"{_NETWORK['host']}:{_NETWORK['port']}",
        )

    def case_data(self, subset: str, case_ids: list[str], payload: dict) -> CaseData:
        partner_map = _fetch_partner_testdata(subset) if subset else {}
        case_data = {}
        hits = 0
        for tc in case_ids:
            row = partner_map.get(tc)
            if isinstance(row, dict):
                case_data[tc] = row
                hits += 1
            else:
                # No seeded row → generate a case-specific stub keyed on the
                # numeric suffix of the case_id. Each RE_XX / PE_XX_CT_USR /
                # etc. gets distinct VPAs / mobile / account so the operator
                # sees meaningful values in RunSubset without seeding api-sim.
                case_data[tc] = _stub_for_case(tc)
        ready = sum(1 for d in case_data.values() if d.get("ready"))
        logger.info(
            "cert_setup_notification: subset=%s cases=%d partner_matches=%d generated=%d ready=%d",
            subset, len(case_ids), hits, len(case_ids) - hits, ready,
        )
        return CaseData(
            case_data=case_data,
            detail=(f" ({hits} sourced from api-sim per-case testdata, "
                    f"{len(case_ids) - hits} generated per-case)"),
        )

    def switch_base(self, product: str, org_id: str) -> Optional[str]:
        if product == "upi" and org_id == _BANK_IDENTITY["participant_code"]:
            return f"https://{_NETWORK['host']}:{_NETWORK['port']}"
        return None

    def bank_org_id(self) -> Optional[str]:
        return str(_BANK_IDENTITY.get("org_id") or "").strip() or None
