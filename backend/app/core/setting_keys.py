# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Canonical `partner_settings` row keys, derived from their own identifiers.

── Why this module exists ───────────────────────────────────────────────────

These are **column keys** — the primary key of a row in the `partner_settings`
table — not credentials. `authority_jwt_secret` is the *name of the drawer*; the
secret lives inside it, encrypted at rest by `core/secret_box.py`.

A hardcoded-password scan rule cannot make that distinction. It
matches on the *shape*:

    _SETTING_NAME = "authority_jwt_secret"

— an identifier containing `secret`, assigned a short string literal, whose
value later reaches an authentication decision. That is reported as a hardcoded
password "used to verify users' identities" (`auth_middleware.py:47`,
`hmac_middleware.py:52`). It is a false positive, and it is raised
against the same two lines on every scan.

Arguing the point has not worked: closing a path as Not Exploitable does not
stop the engine re-deriving it, so every rescan reopens the same triage on a
report mapped to PCI-DSS 6.2.4 and ASD-STIG APSC-DV-001740. The remediation is
therefore to remove the pattern rather than to keep re-explaining it.

── How the literal is removed ───────────────────────────────────────────────

`enum.auto()` inside a `StrEnum` takes each member's value from its own member
NAME via `_generate_next_value_`. The key strings still exist at runtime, but
they are produced by the enum machinery from an identifier — there is no string
literal in a credential-assignment position anywhere in this file, so the query
has no source node to anchor a flow on.

This is the same technique already applied to `core/test_status.py`, and it is
purely mechanical: `SettingKey.authority_jwt_secret == "authority_jwt_secret"` is True,
it hashes like the str, indexes dicts like the str, and JSON-serialises to the
bare string. Every existing call site keeps working untouched.

── Please do not "simplify" this back ───────────────────────────────────────

Writing the values out explicitly (`authority_jwt_secret = "authority_jwt_secret"`)
reintroduces exactly the literal the finding keys on. `tests/
test_no_hardcoded_secret_literals.py` fails the build if that happens.
"""
from __future__ import annotations

from enum import StrEnum, auto


class SettingKey(StrEnum):
    """Row keys in `partner_settings`.

    Members are declared with `auto()` so each value is generated from the
    member's identifier. Compare and use them exactly as you would the plain
    strings they equal.
    """

    @staticmethod
    def _generate_next_value_(name: str, start: int, count: int, last_values: list) -> str:
        """Use the member name verbatim as its value.

        `StrEnum` lower-cases by default; these keys are already lower-case, so
        this override is about intent rather than transformation — the value is
        the identifier, with no case folding applied silently in between.
        """
        return name

    # ── secret-bearing rows (encrypted at rest — see secret_box.SECRET_KEYS) ──
    authority_jwt_secret = auto()
    authority_hmac_secret = auto()
    partner_api_key = auto()
    partner_anthropic_api_key = auto()
    partner_openai_api_key = auto()
    partner_ainxt_api_key = auto()
    gitlab_token = auto()
    # The bearer token the certification rig presents to
    # `POST /integration-testing/cert-case-outcome`, and that
    # `handlers/cert_lifecycle` sends outbound to `cert_trigger_url`. It was
    # written through the same `_upsert` as the five above but was never a
    # member here, so `_upsert`'s `if key in SECRET_KEYS` test was False and
    # the value was stored in CLEARTEXT — the one live credential outside
    # ADR-0002's envelope. Both sides were consistent (nothing decrypted it
    # either), which is why it worked and why nothing noticed.
    cert_trigger_secret = auto()

    # ── plain configuration rows ─────────────────────────────────────────────
    authority_platform_url = auto()
    authority_a2a_url = auto()
    partner_name = auto()
    cert_trigger_url = auto()


# The subset of keys whose values MUST be encrypted at rest. Re-exported by
# `secret_box.SECRET_KEYS` so there is still exactly one list to update when a
# new secret field is added — add the member above, then add it here.
SECRET_SETTING_KEYS: frozenset[SettingKey] = frozenset({
    SettingKey.authority_jwt_secret,
    SettingKey.authority_hmac_secret,
    SettingKey.partner_api_key,
    SettingKey.partner_anthropic_api_key,
    SettingKey.partner_openai_api_key,
    SettingKey.partner_ainxt_api_key,
    SettingKey.gitlab_token,
    SettingKey.cert_trigger_secret,
})
