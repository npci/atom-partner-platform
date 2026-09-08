# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Accept-first aliases on the wire enums.

The Direction and ErrorCode VALUES are pinned — the certification rig matches
`npci_to_bank`/`bank_to_npci` byte-for-byte and partner SOC rules key on the
error strings — so what is EMITTED cannot flip unilaterally. What could move
was acceptance: `_missing_` lets the neutral spellings parse, which is the half
of the rollout that makes the eventual emit-flip deploy-order-independent.

These tests pin all three properties: neutral spellings parse, branded values
are UNCHANGED on emit, and junk is still rejected. If the emit side ever flips
(post rig/SOC migration), the `.value` assertions here invert deliberately.
"""
import pytest

from app.a2a_common.protocol import Direction, ErrorCode


def test_neutral_direction_spellings_parse():
    assert Direction("authority_to_partner") is Direction.AUTHORITY_TO_PARTNER
    assert Direction("partner_to_authority") is Direction.PARTNER_TO_AUTHORITY


def test_branded_direction_spellings_still_parse_and_emit():
    """Stored a2a_messages rows and the rig's comparisons keep working."""
    assert Direction("npci_to_bank") is Direction.AUTHORITY_TO_PARTNER
    assert Direction("bank_to_npci") is Direction.PARTNER_TO_AUTHORITY
    assert Direction.AUTHORITY_TO_PARTNER.value == "npci_to_bank"
    assert Direction.PARTNER_TO_AUTHORITY.value == "bank_to_npci"


def test_neutral_error_code_spellings_parse():
    assert ErrorCode("partner_identity_mismatch") is ErrorCode.BANK_IDENTITY_MISMATCH
    assert ErrorCode("partner_unreachable") is ErrorCode.BANK_UNREACHABLE


def test_branded_error_codes_unchanged_on_emit():
    """Partner SOC detection rules key on these literal strings."""
    assert ErrorCode.BANK_IDENTITY_MISMATCH.value == "bank_identity_mismatch"
    assert ErrorCode.BANK_UNREACHABLE.value == "bank_unreachable"
    # The alias resolves to a member that still carries its layer mapping.
    assert ErrorCode("partner_unreachable").layer == ErrorCode.BANK_UNREACHABLE.layer


def test_unknown_values_still_rejected():
    """`_missing_` must not turn the enum into an accept-anything sink."""
    with pytest.raises(ValueError):
        Direction("sideways")
    with pytest.raises(ValueError):
        ErrorCode("partner_identity_mismatchx")
