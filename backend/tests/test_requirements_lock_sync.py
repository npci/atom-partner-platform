# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""`requirements.txt` and `requirements.lock` must agree.

WHY THIS FILE EXISTS
====================

This repository installs Python dependencies two different ways:

  - `backend/Dockerfile`      -> `pip install --require-hashes -r requirements.lock`
  - `backend/Dockerfile.prod` -> `pip install -r requirements.txt`

So the two files are not documentation of each other; they are each the real
input to a real build. When they disagree, the dev/CI image and the production
image run different code, and the SBOM generated from one does not describe the
other. A scan can then come back clean while production ships the version that
was flagged.

`CONTRIBUTING.md` already names this as "a live source of drift in this
repository", and `backend/Dockerfile` says the commit is failed by
`scripts/ci/hygiene-check.sh` when a pin is missing from the lock. That script
does not exist anywhere in the tree. The stated control was absent, so the
drift it warns about had nothing stopping it. This test is that control,
relocated somewhere that actually runs.

It is also the reason a dependency bump is a deliberate act here rather than a
one-line edit: changing `requirements.txt` alone now fails the suite, which is
exactly the prompt to regenerate the lock (command in that file's header)
instead of shipping a split-brain dependency set.

Standard library only; reads two text files and imports nothing from the app.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
_REQUIREMENTS = _BACKEND / "requirements.txt"
_LOCK = _BACKEND / "requirements.lock"

# `name[extra]==version`, ignoring trailing hash continuations and comments.
_PIN_RE = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)(?P<extra>\[[^\]]+\])?==(?P<version>[^\s\\;]+)")


def _normalise(name: str) -> str:
    """PEP 503 name normalisation.

    `PyJWT`, `pyjwt` and `py-jwt` are one package. Comparing raw strings would
    report a phantom mismatch for a pin that is actually fine, and this test
    has to be trustworthy to be useful.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = _PIN_RE.match(line)
        if match:
            pins[_normalise(match.group("name"))] = match.group("version")
    return pins


@pytest.fixture(scope="module")
def direct_pins() -> dict[str, str]:
    assert _REQUIREMENTS.is_file(), f"missing {_REQUIREMENTS}"
    return _parse_pins(_REQUIREMENTS)


@pytest.fixture(scope="module")
def locked_pins() -> dict[str, str]:
    assert _LOCK.is_file(), f"missing {_LOCK}"
    return _parse_pins(_LOCK)


# ── history note ─────────────────────────────────────────────────────────────
#
# Until 2026-08-30 the lock in this repository was STALE (it predated the
# 2026-08-20 JWT migration: pyjwt absent, python-jose/ecdsa still present,
# fastapi/starlette/pyasn1 behind their bumps) and the two sync tests below
# were xfail(strict=True) tripwires documenting it, plus a sentinel asserting
# the wrong state on purpose. The lock was regenerated with the command in its
# header; per the tripwires' own instructions the xfail markers, the sentinel
# test and the known-drift allowance were then removed. These tests now simply
# enforce sync, which is all they ever wanted to do.


def test_every_direct_pin_appears_in_the_lock(direct_pins, locked_pins):
    """A package in requirements.txt but not the lock installs in prod and not in CI."""
    missing = sorted(set(direct_pins) - set(locked_pins))
    assert not missing, (
        f"{missing} are pinned in requirements.txt but absent from "
        f"requirements.lock. The lock is what backend/Dockerfile installs, so "
        f"these would be missing from the dev/CI image. Regenerate the lock "
        f"(command in its header)."
    )


def test_versions_agree_between_the_two_files(direct_pins, locked_pins):
    """The exact scenario a dependency bump creates if the lock is forgotten."""
    mismatched = {
        name: (direct_pins[name], locked_pins[name])
        for name in sorted(set(direct_pins) & set(locked_pins))
        if direct_pins[name] != locked_pins[name]
    }
    assert not mismatched, (
        "requirements.txt and requirements.lock pin different versions:\n"
        + "\n".join(
            f"  {name}: requirements.txt={req}  lock={lock}"
            for name, (req, lock) in mismatched.items()
        )
        + "\n\nbackend/Dockerfile.prod installs requirements.txt and "
        "backend/Dockerfile installs the lock, so production and CI would run "
        "different code. Regenerate the lock rather than reverting the bump."
    )


def test_the_vulnerable_packages_the_jwt_migration_removed_stay_out(locked_pins):
    """`python-jose` and `ecdsa` must never return to the lock.

    Their removal was the JWT migration's purpose — ecdsa carries
    CVE-2024-23342 (HIGH) with no fixed version, "so the library had to be
    removed rather than pinned around". The regenerated lock dropped both;
    this pin keeps them out.
    """
    assert "python-jose" not in locked_pins and "ecdsa" not in locked_pins, (
        "python-jose/ecdsa are back in requirements.lock — the vulnerable "
        "dependency set the JWT migration removed has been reintroduced."
    )


def test_every_locked_distribution_carries_hashes():
    """A lock entry without hashes silently defeats --require-hashes.

    The whole point of the lock is pinning CONTENT, not just versions. An entry
    that lost its hashes would still install, so nothing else would notice.
    """
    text = _LOCK.read_text(encoding="utf-8")
    blocks = re.split(r"\n(?=[A-Za-z0-9._-]+(?:\[[^\]]+\])?==)", text)
    unhashed = []
    for block in blocks:
        match = _PIN_RE.match(block.strip())
        if not match:
            continue
        # Look only at this pin's own lines, up to the next blank-line gap.
        body = block.split("\n\n", 1)[0]
        if "sha256:" not in body:
            unhashed.append(f"{match.group('name')}=={match.group('version')}")
    assert not unhashed, (
        f"{unhashed} appear in requirements.lock without any sha256 hash, which "
        f"defeats --require-hashes for those distributions."
    )


def test_the_three_annotated_components_are_pinned_where_the_vex_says(direct_pins, locked_pins):
    """Tie the dependency pins to the VEX coordinates.

    `security/vex/partner-platform.vex.json` annotates sqlalchemy 2.0.36 and
    pyjwt 2.13.0 as `not_affected`. That analysis was performed against those
    versions. If a pin moves and this test is not updated alongside the VEX,
    the annotation becomes a statement about a version nobody reviewed.

    `backend/tests/test_vex_document.py` asserts the same coordinates from the
    VEX side, so the two files cannot drift apart in either direction.
    """
    expected = {"sqlalchemy": "2.0.36", "pyjwt": "2.13.0"}
    for name, version in expected.items():
        assert direct_pins.get(name) == version, (
            f"{name} is pinned at {direct_pins.get(name)} in requirements.txt but "
            f"the VEX analysis was performed against {version}. Re-run the "
            f"analysis for the new version and update the VEX, or revert the pin."
        )

    for name, version in expected.items():
        assert locked_pins.get(name) == version, (
            f"{name} is locked at {locked_pins.get(name)}, but the VEX "
            f"analysis was performed against {version}."
        )
