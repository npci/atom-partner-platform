# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Load and validate a domain pack from YAML.

A pack is the DOMAIN'S VOCABULARY: what to call the authority, what kind of
organisation this partner is, what roles it can play and what identifiers those
roles carry. It is pure data — a YAML file cannot express behaviour, and that
is deliberate.

WHAT THIS IS NOT: `data/partner_profile.md` (PARTNER_PROFILE_PATH, config.py).
That file is THIS DEPLOYMENT'S CAPABILITY DATA — which systems this particular
organisation runs, what it has already rolled out — and it is authored by the
operator. The pack is the domain's vocabulary and ships WITH the domain. It is
tempting to hang vocabulary off the profile's frontmatter and avoid a second
mechanism; don't. Merging them makes every operator re-author vocabulary that
should have arrived in the box.

DELIBERATELY SMALLER THAN THE AUTHORITY'S PACK. The authority platform's
`core/domain/config_pack.py` also carries participants, cert vocabulary, role
scopes, error-code tables and artifact specs, because it IS the authority and
generates all of that. This side consumes changes and answers them. Prompt
blocks plus a role/identifier schema plus sign-off identity is enough; resist
growing this to match.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence


class PackError(RuntimeError):
    """A pack file is missing, unparseable, or structurally wrong.

    Deliberately loud, and never caught into a default. A deployment that
    intended one domain's vocabulary but silently got another's would generate
    subtly wrong prose in documents that leave the building — a failure that
    surfaces as an embarrassing PDF, not as a stack trace. Fail at boot instead.
    """


@dataclass(frozen=True)
class RoleField:
    """One identifier a role must supply before a certification run."""

    key: str
    label: str
    placeholder: str = ""
    required: bool = False


@dataclass(frozen=True)
class Role:
    """A role this partner can play in the ecosystem.

    `case_prefix` is the test-case id prefix the authority uses for cases this
    role must run (UPI: PR_/PE_/RE_/BE_). It is domain data, not display text.
    """

    key: str
    label: str
    case_prefix: str = ""
    fields: Sequence[RoleField] = field(default_factory=tuple)


@dataclass(frozen=True)
class Signoff:
    """Identity printed on the certification sign-off PDF.

    Every field here — including `accent`, which is a brand colour — belongs to
    the deployment, not to this file. None of it may be hardcoded in
    `services/cert_signoff_pdf.py`.

    `authority_short` / `partner_short` label the results table's "initiated by"
    column, which maps the WIRE tokens (NPCI, BANK — pinned, never renamed) to
    something printable. They are separate from `issuer` because that column is
    12mm wide: a full legal entity name does not fit where a four-letter token
    did.

    `issued_at` is the place of issue printed in the attestation block. It sits
    in the document FOOTER, which is easy to miss when checking the body. Empty
    means omit — the same contract as `issuer`: a deployment that has not said
    where it signs from must not have a city invented for it.

    There is deliberately no separate filename field. `document_prefix()`
    derives it from `programme`, so the name on disk cannot drift from the name
    printed on the page.
    """

    issuer: str = ""
    programme: str = ""
    signatory: str = ""
    accent: str = "#334155"
    authority_short: str = "AUTHORITY"
    partner_short: str = "PARTNER"
    issued_at: str = ""

    def document_prefix(self) -> str:
        """Leading token for a generated document's filename.

        Falls back to a neutral "Certification" so an unconfigured deployment
        still downloads something self-describing rather than a bare id — the
        filename is the only label the file carries once it leaves the browser.

        Sanitised because this value reaches a `Content-Disposition` header: a
        pack is operator-supplied config, and a quote or newline in it would
        otherwise let the pack author steer that header. Callers additionally
        urlquote the assembled name, which percent-encodes anything non-ASCII.

        The filter is UNICODE-AWARE (`str.isalnum()`), not `[A-Za-z0-9]`. An
        ASCII-only class silently deleted every character of a non-Latin
        programme name — `認証プログラム` and `Πιστοποίηση` both collapsed to the
        empty string and fell through to "Certification", so a fully configured
        deployment got the unconfigured fallback and the filename stopped
        matching the masthead. That is the exact drift this method exists to
        prevent, and it hit configured packs rather than unconfigured ones.
        `-` is preserved for the same reason: dropping it renamed
        `NLLN_Lending-Protocol_Certification`.
        """
        cleaned = "".join(
            c if (c.isalnum() or c in "-_") else "_" for c in (self.programme or "")
        ).strip("_")
        return cleaned or "Certification"


@dataclass(frozen=True)
class DomainPack:
    name: str
    source_path: str
    _prompt_blocks: Mapping[str, str]
    roles: Sequence[Role]
    signoff: Signoff
    # The fallback certification profile submitted when the operator has stored
    # no `cert_config`. Free-form because its KEYS are the A2A spec's
    # `cert_config_submission` shape (wire contract, not ours to rename) while
    # its VALUES are pure domain data.
    #
    # Empty for a pack that supplies none, and that is the point. This was a
    # hardcoded participant identity in the handler, merged UNDER every
    # submission — so even a configured deployment transmitted one domain's
    # identifiers for whichever keys it had not overridden. A pack with no
    # profile now submits only what the operator actually configured. See
    # `a2a_common/handlers/cert_lifecycle.py` for the full history.
    cert_profile: Mapping[str, Any] = field(default_factory=dict)
    # Per-case values sent when a case has no stored `change_test_data` row.
    # Empty by default for the same reason, and with the same consequence: no
    # `ready: true`, so a case reports not-ready instead of executing against
    # values nobody configured.
    cert_case_data: Mapping[str, Any] = field(default_factory=dict)

    def prompt_block(self, name: str, default: str = "") -> str:
        """Return a named prose block, or `default` if the pack omits it.

        NEVER raises on a missing name. A domain with no separate settlement
        role supplies no "settlement" block, and that is a valid pack, not a
        broken one. Callers must always pass a sensible `default` so an
        incomplete pack degrades to generic wording rather than to a KeyError
        halfway through building a prompt.
        """
        return self._prompt_blocks.get(name, default)

    def prompt_blocks(self) -> Mapping[str, str]:
        """Every block, for callers that interpolate the whole set at once.

        `agents/prompts.load_prompt` uses this to make all vocabulary available
        to every prompt as `$block_name` without callers passing anything.
        """
        return dict(self._prompt_blocks)

    def role(self, key: str) -> Role | None:
        return next((r for r in self.roles if r.key == key), None)

    def role_label(self, key: str | None) -> str:
        """Display label for a role key, falling back to the key itself.

        A cert run recorded under a role the current pack no longer defines
        must still render — showing the raw key is worse than a nice label but
        much better than an empty cell on a signed document.
        """
        k = (key or "").strip()
        if not k:
            return "—"
        r = self.role(k)
        return r.label if r else k

    def field_label(self, key: str) -> str:
        """Display label for a test-data identifier, across all roles.

        The sign-off PDF renders whatever identifiers a cert run collected, and
        it does not know which role they came from — REMITTER_BANK and
        BENEFICIARY_BANK both supply `account_number`. First definition wins;
        packs that label the same key differently per role would be ambiguous
        here anyway.

        Falls back to a humanised key so an identifier the pack does not
        describe still appears, rather than being silently dropped from a
        document someone signs.
        """
        for role in self.roles:
            for f in role.fields:
                if f.key == key:
                    return f.label
        return key.replace("_", " ").strip().capitalize()


def _require_mapping(value: Any, where: str, source: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PackError(f"{source}: `{where}` must be a mapping, got {type(value).__name__}")
    return value


# The sign-off table's "initiated by" column, in points, less its cell padding
# (22mm column, 5pt padding each side) at the 8.5pt Helvetica the cell renders
# in. Kept in sync with `services/cert_signoff_pdf.py`; a test renders real PDFs
# and asserts the two agree, because a silent drift here is only visible on a
# signed document.
_INITIATOR_COL_PT = 22 * 72 / 25.4 - 10
_INITIATOR_FONT = ("Helvetica", 8.5)


def _check_fits_initiator_column(value: str, field: str, source: str) -> None:
    """Reject a label that will word-wrap in the sign-off PDF.

    MEASURED, NOT COUNTED. A character cap is the wrong check: Helvetica is
    proportional, so "REGISTRAR" fits the column and "OMBUDSMAN" — same nine
    characters — does not. reportlab word-wraps rather than truncating, so the
    failure renders as stacked fragments ("OMBUD" -> "OMB"/"UD") on a document
    someone signs. Catch it here, where the pack author can still fix it,
    rather than leaving it for whoever opens the PDF.

    reportlab is imported locally: it is already a hard dependency of this
    backend, but pack loading should not drag a PDF engine in on every import.
    If it is somehow unavailable the check is skipped rather than made fatal —
    a missing renderer must not stop a pack loading for the many callers that
    never produce a PDF.
    """
    try:
        from reportlab.pdfbase.pdfmetrics import stringWidth
    except Exception:  # noqa: BLE001 - see docstring
        return
    width = stringWidth(value, *_INITIATOR_FONT)
    if width > _INITIATOR_COL_PT:
        raise PackError(
            f"{source}: signoff.{field}={value!r} renders {width:.1f}pt wide but "
            f"the sign-off PDF's initiator column fits {_INITIATOR_COL_PT:.1f}pt. "
            f"reportlab word-wraps rather than truncating, so this would print as "
            f"stacked fragments. Use a shorter label."
        )


def load(path: str | Path) -> DomainPack:
    """Parse and validate the pack at `path`."""
    import yaml  # local import: keeps module import cheap for callers that never load

    p = Path(path)
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PackError(f"{p}: no such pack file") from exc
    except yaml.YAMLError as exc:
        raise PackError(f"{p}: not valid YAML — {exc}") from exc

    if not isinstance(raw, dict):
        raise PackError(f"{p}: a pack must be a YAML mapping, got {type(raw).__name__}")

    name = str(raw.get("pack") or p.stem)

    blocks_raw = _require_mapping(raw.get("prompt_blocks"), "prompt_blocks", str(p))
    # Coerced to str so a YAML scalar that parses as a number or bool (a version
    # like `2.0`, a bare `yes`) still interpolates into a prompt as text rather
    # than rendering as "True" or blowing up on concatenation.
    blocks = {str(k): ("" if v is None else str(v)) for k, v in blocks_raw.items()}

    roles_raw = raw.get("roles") or []
    if not isinstance(roles_raw, list):
        raise PackError(f"{p}: `roles` must be a list, got {type(roles_raw).__name__}")

    roles: list[Role] = []
    seen: set[str] = set()
    for i, r in enumerate(roles_raw):
        if not isinstance(r, dict):
            raise PackError(f"{p}: roles[{i}] must be a mapping, got {type(r).__name__}")
        key = str(r.get("key") or "").strip()
        if not key:
            raise PackError(f"{p}: roles[{i}] is missing a `key`")
        if key in seen:
            # A duplicate key would make role(key) silently return the first and
            # ignore the second's identifier fields — a cert run would then be
            # collected against the wrong schema.
            raise PackError(f"{p}: duplicate role key {key!r}")
        seen.add(key)

        fields_raw = r.get("fields") or []
        if not isinstance(fields_raw, list):
            raise PackError(f"{p}: roles[{i}].fields must be a list")
        fields: list[RoleField] = []
        for j, f in enumerate(fields_raw):
            if not isinstance(f, dict):
                raise PackError(f"{p}: roles[{i}].fields[{j}] must be a mapping")
            fkey = str(f.get("key") or "").strip()
            if not fkey:
                raise PackError(f"{p}: roles[{i}].fields[{j}] is missing a `key`")
            fields.append(RoleField(
                key=fkey,
                label=str(f.get("label") or fkey),
                placeholder=str(f.get("placeholder") or ""),
                required=bool(f.get("required", False)),
            ))

        roles.append(Role(
            key=key,
            label=str(r.get("label") or key),
            case_prefix=str(r.get("case_prefix") or ""),
            fields=tuple(fields),
        ))

    s = _require_mapping(raw.get("signoff"), "signoff", str(p))
    for fld in ("authority_short", "partner_short"):
        v = s.get(fld)
        if v is not None:
            _check_fits_initiator_column(str(v), fld, str(p))

    signoff = Signoff(
        issuer=str(s.get("issuer") or ""),
        programme=str(s.get("programme") or ""),
        signatory=str(s.get("signatory") or ""),
        accent=str(s.get("accent") or "#334155"),
        authority_short=str(s.get("authority_short") or "AUTHORITY"),
        partner_short=str(s.get("partner_short") or "PARTNER"),
        issued_at=str(s.get("issued_at") or ""),
    )

    # Validated only as "a mapping if present" — the keys are the A2A spec's,
    # not this loader's, so type-checking them here would just be a second,
    # drifting copy of the counterparty's schema.
    cert_profile = _require_mapping(raw.get("cert_profile"), "cert_profile", str(p))
    cert_case_data = _require_mapping(
        raw.get("cert_case_data"), "cert_case_data", str(p))

    return DomainPack(
        name=name,
        source_path=str(p),
        _prompt_blocks=blocks,
        roles=tuple(roles),
        signoff=signoff,
        cert_profile=dict(cert_profile),
        cert_case_data=dict(cert_case_data),
    )
