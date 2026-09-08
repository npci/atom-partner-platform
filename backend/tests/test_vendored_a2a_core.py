# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Drift tripwire for the vendored a2a-core files.

Five files in `app/a2a_common/` declare themselves GENERATED and point at
`packages/a2a-core/` in the Authority repository, to be re-synced with
`scripts/ci/sync-a2a-core.sh`. Neither the directory nor the script exists
here, and neither does `tests/a2a_common/test_protocol_contract.py`, which
`protocol.py`'s docstring names as its own drift test.

So the repository asserted three enforcement mechanisms for its
security-critical wire code and shipped none of them. The lockfile got a
sync test; the HMAC signer and the protocol did not. `protocol.py` then
drifted — see `app/a2a_common/VENDORED.md` — while two commit messages
asserted zero drift, one of them for a file that commit never touched.

This does NOT verify the content matches upstream; nothing in this repository
can. It pins what is here, so a silent edit becomes a failing test and a
deliberate one has to say why.
"""
import hashlib
from pathlib import Path

import pytest

_VENDORED_DIR = Path(__file__).resolve().parents[1] / "app" / "a2a_common"

# Keep in step with the table in VENDORED.md — update both in the same commit
# as the file itself, never the hash alone.
EXPECTED_SHA256 = {
    # Bumped 2026-09-07 alongside the file: `LEGACY_HEADER_PREFIX` +
    # `emit_legacy_headers()`. This synced the copy TOWARD the authority's,
    # which already had the prefix constant — the drift was the partner's.
    "hmac_signer.py": "b11d631b576ce6bc509fcd3e740f62ce6f30f996bc0851fbbe2ef519bd122dc1",
    # Bumped 2026-09-07: accept-first `_missing_` aliases on Direction and
    # ErrorCode (neutral spellings parse; branded values still emitted). The
    # same block must land upstream via apply-authority-wire-dual-accept.py —
    # see VENDORED.md.
    "protocol.py": "c9128a126ab639679980ea5b12f545701adfe1bc3382fae4862b1d6fdbb11d27",
    "integration_contract.py": "7757207d5bd757028f59ebc11d8cd6f7a1daf212877caf427cb08576306b5e12",
    "integration_allowlist.py": "d837cb73ef21e53b99e3adcd0e8ea7a48f7a3bf5fdd75083e332b23a63a93aa4",
    "executor_base.py": "cdf1cd6440a63970228a798c4efdcab1a04588e6428d44de314917254f32e860",
}

_HEADER_MARKER = ">>> a2a-core vendored header >>>"


@pytest.mark.parametrize("name", sorted(EXPECTED_SHA256))
def test_vendored_file_has_not_drifted(name):
    path = _VENDORED_DIR / name
    assert path.exists(), f"{name} is missing from app/a2a_common/"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == EXPECTED_SHA256[name], (
        f"{name} changed.\n"
        f"  expected {EXPECTED_SHA256[name]}\n"
        f"  actual   {digest}\n"
        "This file declares itself GENERATED from the Authority's "
        "packages/a2a-core/. If the change came from a re-sync, update the hash "
        "here AND the table in app/a2a_common/VENDORED.md in the same commit, "
        "naming the upstream revision. If it is a local edit, say so there — "
        "protocol.py already carries an undocumented one."
    )


@pytest.mark.parametrize("name", sorted(EXPECTED_SHA256))
def test_vendored_header_is_intact(name):
    """The header is the only thing telling the next reader not to edit here."""
    text = (_VENDORED_DIR / name).read_text(encoding="utf-8")
    assert _HEADER_MARKER in text, (
        f"{name} lost its vendored header — a future reader has no signal that "
        "editing it locally will be overwritten by a re-sync."
    )


def test_the_hash_table_covers_every_vendored_file():
    """A sixth vendored file must not slip in unpinned."""
    on_disk = {
        p.name for p in _VENDORED_DIR.glob("*.py")
        if _HEADER_MARKER in p.read_text(encoding="utf-8")
    }
    assert on_disk == set(EXPECTED_SHA256), (
        "vendored files on disk do not match the pinned set. "
        f"unpinned: {sorted(on_disk - set(EXPECTED_SHA256))}, "
        f"pinned but absent: {sorted(set(EXPECTED_SHA256) - on_disk)}"
    )


def test_provenance_record_exists():
    """VENDORED.md is the only place the missing sync tooling is written down."""
    assert (_VENDORED_DIR / "VENDORED.md").exists()
