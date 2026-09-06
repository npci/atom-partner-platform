#!/usr/bin/env python3
# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Merge VEX analysis decisions into a generated CycloneDX SBOM.

WHY THIS EXISTS
===============

A VEX document changes a scan result only if the scanner ingests it. Writing an
accurate ``not_affected`` analysis and leaving it in the repository accomplishes
nothing: the finding still reports as ``ANALYSIS STATE: Unannotated``, because
nothing ever told the scanner otherwise.

The A2A Compliance Report of 2026-08-27 reported three Security policy
violations in exactly that state. ``security/vex/partner-platform.vex.json``
records the triage for all three. This script is the bridge between the two.

Some platforms accept a VEX file directly alongside the SBOM (a ``--vex`` flag
or equivalent). Prefer that when it exists -- it keeps the analysis in its own
reviewable artifact. Use this script for the common case where the platform
consumes only ONE document, so the annotations have to travel inside the SBOM
itself.

WHAT IT DOES
============

Reads a CycloneDX SBOM and the VEX document, and writes a copy of the SBOM with
the VEX ``vulnerabilities`` entries merged into it. It is deliberately strict:
an annotation that silently fails to attach is worse than no annotation, because
the report still shows the finding while everyone believes it was handled.

So every VEX entry MUST resolve to a component that is actually in the SBOM. If
one does not, this script exits non-zero and explains which one, rather than
writing a document that looks fine and annotates nothing.

MATCHING IS BY PURL, NOT BY bom-ref
===================================

``bom-ref`` values are internal to whichever tool generated the document, so
they differ between the VEX and the SBOM. Package URLs are the stable identity
that both sides share.

purls are compared after normalisation, because the same package legitimately
appears under different spellings:

  - The PyPI purl type requires a lowercase, normalised name, and treats ``-``,
    ``_`` and ``.`` as equivalent. ``pkg:pypi/PyJWT`` and ``pkg:pypi/pyjwt`` are
    the same package; a naive string comparison says they are not. That exact
    mismatch was present in an earlier revision of the VEX and would have caused
    the pyjwt annotation to attach to nothing.
  - Qualifiers and subpaths (``?arch=...``, ``#subdir``) are not part of package
    identity for this purpose and are stripped before comparison.

Version is compared exactly. Annotating a version you did not analyse is not a
convenience, it is a false statement.

USAGE
=====

    python scripts/merge_vex.py \\
        --sbom sbom.json \\
        --vex security/vex/partner-platform.vex.json \\
        --out sbom.vex.json

Exit codes:
    0  merged cleanly
    1  usage or I/O error
    2  at least one VEX entry did not resolve to an SBOM component

The non-zero exit on an unresolved entry is the point of the script. Wire it
into the pipeline BEFORE upload so a stale annotation -- one naming a package
that has since been removed or upgraded -- breaks the build instead of quietly
becoming a lie in an audit record.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote

# ── purl normalisation ───────────────────────────────────────────────────────

# `pkg:TYPE/NAMESPACE/NAME@VERSION?QUALIFIERS#SUBPATH`, of which we need the
# type, the name (with optional namespace) and the version.
_PURL_RE = re.compile(
    r"^pkg:(?P<type>[^/]+)/(?P<name>[^@?#]+)(?:@(?P<version>[^?#]+))?"
)


def normalise_purl(purl: str) -> tuple[str, str, str] | None:
    """Return ``(type, name, version)`` in comparable form, or None if unparsable.

    Applies the type-specific rules that matter for the ecosystems this project
    uses. Anything not explicitly handled is lowercased on type only, which is
    always safe (the purl spec makes the type case-insensitive) and leaves the
    name untouched, which is the conservative choice: a false MISMATCH surfaces
    as a loud unresolved-entry error, whereas a false MATCH would attach an
    analysis to the wrong package.
    """
    m = _PURL_RE.match(purl.strip())
    if not m:
        return None

    ptype = m.group("type").lower()
    name = unquote(m.group("name"))
    version = unquote(m.group("version") or "")

    if ptype == "pypi":
        # PEP 503 normalisation: case-insensitive, and runs of `-`, `_` and `.`
        # are equivalent. This is what makes `PyJWT` == `pyjwt`.
        name = re.sub(r"[-_.]+", "-", name).lower()
    elif ptype == "npm":
        # npm names are lowercase; scopes (`@scope/name`) are preserved as-is.
        name = name.lower()
    elif ptype in {"golang", "github", "bitbucket"}:
        name = name.lower()

    return ptype, name, version


def _component_purls(component: dict[str, Any]) -> list[str]:
    """Every purl a component can be identified by.

    Usually just ``purl``, but some generators put the coordinates only in
    ``bom-ref``, so that is accepted as a fallback when it looks like a purl.
    """
    purls: list[str] = []
    if isinstance(component.get("purl"), str):
        purls.append(component["purl"])
    ref = component.get("bom-ref")
    if isinstance(ref, str) and ref.startswith("pkg:") and ref not in purls:
        purls.append(ref)
    return purls


def _walk_components(components: list[dict[str, Any]]):
    """Yield every component, descending into nested ``components`` arrays.

    CycloneDX allows components to nest. A flat scan would miss a transitive
    dependency expressed as a child, and the annotation for it would silently
    fail to attach.
    """
    for comp in components or []:
        if not isinstance(comp, dict):
            continue
        yield comp
        nested = comp.get("components")
        if isinstance(nested, list):
            yield from _walk_components(nested)


# ── merge ────────────────────────────────────────────────────────────────────


def build_index(sbom: dict[str, Any]) -> dict[tuple[str, str, str], str]:
    """Map normalised purl -> the SBOM's own ``bom-ref`` for that component."""
    index: dict[tuple[str, str, str], str] = {}

    candidates = list(_walk_components(sbom.get("components", [])))
    meta_component = sbom.get("metadata", {}).get("component")
    if isinstance(meta_component, dict):
        candidates.append(meta_component)

    for comp in candidates:
        bom_ref = comp.get("bom-ref")
        if not isinstance(bom_ref, str):
            continue
        for purl in _component_purls(comp):
            key = normalise_purl(purl)
            if key is not None:
                # First writer wins: a component listed twice is the same
                # package, and the earlier entry is the one nearer the root.
                index.setdefault(key, bom_ref)

    return index


def merge(
    sbom: dict[str, Any], vex: dict[str, Any]
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Merge VEX vulnerabilities into ``sbom``.

    Returns ``(merged_sbom, applied_messages, unresolved_messages)``. The input
    ``sbom`` is not mutated.
    """
    merged = json.loads(json.dumps(sbom))  # deep copy; inputs stay untouched
    index = build_index(merged)

    existing: dict[str, dict[str, Any]] = {}
    for vuln in merged.get("vulnerabilities", []) or []:
        if isinstance(vuln, dict) and isinstance(vuln.get("id"), str):
            existing[vuln["id"]] = vuln

    applied: list[str] = []
    unresolved: list[str] = []
    out_vulns: list[dict[str, Any]] = list(merged.get("vulnerabilities", []) or [])

    for vuln in vex.get("vulnerabilities", []) or []:
        vid = vuln.get("id", "<no id>")
        affects = vuln.get("affects") or []
        if not affects:
            unresolved.append(f"{vid}: VEX entry has no 'affects' array")
            continue

        # Re-point each affects[].ref at the SBOM's own bom-ref for that package.
        resolved_refs: list[dict[str, Any]] = []
        failed = False
        for affect in affects:
            ref = affect.get("ref", "")
            key = normalise_purl(ref) if isinstance(ref, str) else None
            if key is None:
                unresolved.append(f"{vid}: affects[].ref is not a parsable purl: {ref!r}")
                failed = True
                continue
            target = index.get(key)
            if target is None:
                ptype, name, version = key
                same_name = sorted(
                    v for (t, n, v) in index if t == ptype and n == name
                )
                hint = (
                    f" -- SBOM has {name} at version(s) {', '.join(same_name)}"
                    if same_name
                    else f" -- SBOM contains no component named {name!r} of type {ptype!r}"
                )
                unresolved.append(f"{vid}: no SBOM component matches {ref}{hint}")
                failed = True
                continue
            resolved_refs.append({**affect, "ref": target})

        if failed:
            continue

        entry = json.loads(json.dumps(vuln))
        entry["affects"] = resolved_refs

        if vid in existing:
            # The scanner already reported this vulnerability. Attach OUR
            # analysis to ITS entry rather than appending a duplicate -- a second
            # entry for the same id is what leaves a finding showing as
            # Unannotated while a correct annotation sits beside it, ignored.
            target_entry = existing[vid]
            if "analysis" in entry:
                target_entry["analysis"] = entry["analysis"]
            if "affects" in entry and not target_entry.get("affects"):
                target_entry["affects"] = entry["affects"]
            applied.append(f"{vid}: analysis attached to existing SBOM entry")
        else:
            out_vulns.append(entry)
            applied.append(f"{vid}: added as a new SBOM vulnerability entry")

    merged["vulnerabilities"] = out_vulns
    return merged, applied, unresolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Merge VEX analysis decisions into a CycloneDX SBOM.",
    )
    parser.add_argument("--sbom", required=True, type=Path, help="input CycloneDX SBOM (JSON)")
    parser.add_argument("--vex", required=True, type=Path, help="input VEX document (CycloneDX JSON)")
    parser.add_argument("--out", required=True, type=Path, help="output path for the merged SBOM")
    parser.add_argument(
        "--allow-unresolved",
        action="store_true",
        help=(
            "write the merged SBOM and exit 0 even if some VEX entries did not "
            "resolve. Off by default on purpose: an annotation that attaches to "
            "nothing looks handled and is not."
        ),
    )
    args = parser.parse_args(argv)

    try:
        sbom = json.loads(args.sbom.read_text(encoding="utf-8"))
        vex = json.loads(args.vex.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    merged, applied, unresolved = merge(sbom, vex)

    for line in applied:
        print(f"  ok  {line}")
    for line in unresolved:
        print(f"  FAIL {line}", file=sys.stderr)

    if unresolved and not args.allow_unresolved:
        print(
            f"\n{len(unresolved)} VEX entr{'y' if len(unresolved) == 1 else 'ies'} "
            "did not resolve; refusing to write a merged SBOM that silently "
            "annotates nothing. Fix the purl or withdraw the annotation.",
            file=sys.stderr,
        )
        return 2

    try:
        args.out.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"\nwrote {args.out} ({len(applied)} annotation(s) applied)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
