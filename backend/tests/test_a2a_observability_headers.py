# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The correlation headers on every outbound A2A call.

These were the last branded headers on this wire. The HMAC envelope headers got
a dual-emit rollout (`X-Auth-*` sent, `X-NPCI-*` accepted and optionally still
sent); these two did not, so every outbound call from every deployment carried
one organisation's namespace into the receiver's access logs.

Nothing READS them — they exist for nginx/WAF/APM correlation — which is what
makes them safe to move without a coordinated deploy, and also why nothing
failed while they were wrong.
"""
import importlib

from app.a2a_common.client import (
    HEADER_CHANGE_ID,
    HEADER_CORRELATION_ID,
    LEGACY_HEADER_CHANGE_ID,
    LEGACY_HEADER_CORRELATION_ID,
    observability_headers,
)


def _reload_with(monkeypatch, value: str | None):
    """Re-import the signer so the module-level env read is re-evaluated."""
    import app.a2a_common.client as client
    import app.a2a_common.hmac_signer as signer

    if value is None:
        monkeypatch.delenv("A2A_EMIT_LEGACY_HEADERS", raising=False)
    else:
        monkeypatch.setenv("A2A_EMIT_LEGACY_HEADERS", value)
    importlib.reload(signer)
    importlib.reload(client)
    return client


def test_the_neutral_names_are_the_canonical_ones():
    assert HEADER_CORRELATION_ID == "X-Auth-Correlation-ID"
    assert HEADER_CHANGE_ID == "X-Auth-Change-ID"


def test_legacy_names_share_the_envelope_prefix():
    """One retargetable namespace, not a second literal.

    An adopter changing `hmac_signer.LEGACY_HEADER_PREFIX` must move these too;
    otherwise three of the five legacy headers follow and two do not.
    """
    from app.a2a_common.hmac_signer import LEGACY_HEADER_PREFIX

    assert LEGACY_HEADER_CORRELATION_ID == f"{LEGACY_HEADER_PREFIX}-Correlation-ID"
    assert LEGACY_HEADER_CHANGE_ID == f"{LEGACY_HEADER_PREFIX}-Change-ID"


def test_both_spellings_are_emitted_by_default():
    """Default ON: an upgrade must not silently stop feeding somebody's APM."""
    out = observability_headers("corr-1", {"change_id": "chg-9"})

    assert out[HEADER_CORRELATION_ID] == "corr-1"
    assert out[HEADER_CHANGE_ID] == "chg-9"
    assert out[LEGACY_HEADER_CORRELATION_ID] == "corr-1"
    assert out[LEGACY_HEADER_CHANGE_ID] == "chg-9"


def test_legacy_half_drops_when_the_operator_says_so(monkeypatch):
    """The same switch that governs the envelope headers governs these.

    An operator who has retargeted their log rules flips one variable and every
    legacy header stops — they should not have to discover that two of the five
    were wired to something else.
    """
    client = _reload_with(monkeypatch, "0")
    try:
        out = client.observability_headers("corr-1", {"change_id": "chg-9"})

        assert out == {
            client.HEADER_CORRELATION_ID: "corr-1",
            client.HEADER_CHANGE_ID: "chg-9",
        }
        assert not any("NPCI" in k for k in out)
    finally:
        _reload_with(monkeypatch, None)


def test_absent_values_emit_no_header_at_all():
    """No correlation id and no change_id means no headers, not empty ones."""
    assert observability_headers(None, None) == {}
    assert observability_headers(None, {}) == {}
    assert observability_headers("", {"change_id": ""}) == {}


def test_non_dict_payload_does_not_raise():
    """`data` is caller-supplied and not always a mapping."""
    assert observability_headers("corr-1", "not-a-dict") == {
        HEADER_CORRELATION_ID: "corr-1",
        LEGACY_HEADER_CORRELATION_ID: "corr-1",
    }
