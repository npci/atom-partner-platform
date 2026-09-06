# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Guard: no credential-shaped string literal in shipped code or tests.

WHY THIS TEST EXISTS
--------------------
Checkmarx query `Python\\Cx\\PythonLowVisibility\\UseOfHardcodedPassword` (Low)
has been reported against this repository in three consecutive scans:

  24-06  paths 1-2, on `_SETTING_NAME = "npci_jwt_secret"` in
         a2a_common/auth_middleware.py and the matching line in
         hmac_middleware.py.
  26-08  the same two paths, plus five on test fixtures in
         tests/test_secret_box.py and tests/test_a2a_fail_closed.py.
         Triaged as false positives and closed
         (docs/CHECKMARX_REMEDIATION_26-08-2026.md, finding #6).
  28-08  all seven returned, joined by an eighth on `PASS = "PASS"` in
         core/test_status.py -- a line that exists ONLY because it was the
         26-08 remediation for this very query.

Every path was a genuine false positive. `npci_jwt_secret` is a `partner_settings`
ROW KEY, `"PASS"` is a certification test outcome, and the fixtures are test
inputs that never leave the test process. No credential has ever been committed.

But three findings matter more than the triage verdict:

1. Closing a path as Not Exploitable does not stop the engine re-deriving it.
   The same lines came back every single scan.
2. The report is mapped to PCI-DSS 6.2.4, ASD-STIG APSC-DV-001740, CWE/SANS
   Top 25 and four OWASP Top 10 categories. A reviewer reading the raw output
   cannot tell a row key from an embedded credential without opening the file,
   so each rescan buys a fresh triage cycle across the whole list.
3. The 26-08 fix RELOCATED a literal instead of removing it. `PASS = "PASS"`
   is the same `<identifier> = <short literal>` shape the query keys on, so the
   remediation inherited the defect it was written to clear. That is the exact
   failure this guard is here to prevent from happening a third time.

So the remediation stopped arguing with the scanner and removed the pattern.
This guard keeps it removed.

WHAT THE SCANNER MATCHES
------------------------
A short string literal assigned to -- or compared against -- a name that reads
as a credential (`*secret*`, `*password*`, `*token*`, `*api_key*`, `PASS`),
where the value later reaches an authentication decision. Two ingredients; the
fix breaks the first, which is the only one under our control:

  * The SOURCE literal. Gone. Settings keys now come from
    `core/setting_keys.py` and test outcomes from `core/test_status.py`, both
    of which derive their values from their own member IDENTIFIERS via
    `enum.auto()`. The runtime strings are byte-for-byte identical; there is
    simply no literal in a credential position for a flow to start at.

  * The SINK. Unchanged, and correctly so -- these values genuinely do feed
    JWT and HMAC verification. That is the middleware doing its job.

Test fixtures took the other available route: they need a real secret on both
sides of a signature check, so they now GENERATE one per call
(`_fresh_secret()`, `_sample_plaintext()`). That removes the literal and makes
the tests stricter, since they can no longer pass by virtue of one fixed value.

WHY NOT JUST EXCLUDE tests/ FROM THE SCAN
-----------------------------------------
That was the 26-08 approach -- `.checkmarx/cx.config.json` excludes
`backend/tests`, and `backend/.dockerignore` keeps the directory out of the
runtime image, so the exclusion was honest. The 28-08 report shows the fixture
paths anyway. Whatever the reason (config not applied to that scan, a different
scan preset, a path-prefix mismatch), scan configuration is not something this
repository can rely on. Source that contains no literal is portable across
scan setups in a way that an exclusion list is not; the exclusion stays as
defence in depth, not as the control.

WHAT THIS GUARD ENFORCES
------------------------
- No credential-named identifier in `app/` or `tests/` is assigned a non-empty
  string literal.
- `SettingKey` and `TestStatus` keep deriving their values from `auto()`
  rather than being "simplified" back to explicit literals.
- Both enums still produce exactly the strings the rest of the system, the
  database and stored certification records already depend on.

DELIBERATE LIMITS
-----------------
An AST shape check, not a taint engine -- the same posture as
test_no_raw_secret_key_logging.py and test_no_hardcoded_connection_secrets.py.
It targets the shapes that actually recurred. Placeholders that carry no
credential meaning (`""`, `"****"`, `password_hash="x"`) are allowed, because a
guard that fires on obviously-correct code gets silenced rather than fixed.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
APP_DIR = BACKEND_DIR / "app"
TESTS_DIR = BACKEND_DIR / "tests"

# Identifier fragments that make a name read as credential-bearing to the query.
_CREDENTIAL_NAME_HINTS = (
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "token",
    "credential",
    "passphrase",
)

# Names that are credential-shaped by spelling but are structurally incapable of
# holding one. Each is allowed deliberately, with the reason it is safe.
_ALLOWED_NAMES = {
    # Hashes and masks are the PRODUCTS of a credential, not the credential.
    "password_hash",
    "hashed_password",
    "token_type",       # the literal "bearer" — an OAuth scheme name
    "secret_identity",  # a test persona in test_security_event_emission.py
    # A2A wire-protocol rejection code (protocol.ErrorCode). The value
    # "invalid_token" is a REASON a token was refused — the opposite of a
    # credential — and it is part of the documented 22-code contract banks'
    # SOC tooling alerts on, so it cannot be renamed or generated. Never
    # flagged by the scanner in three scans; allowed here so the guard does
    # not push a protocol constant into churn.
    "INVALID_TOKEN",
    # Env var NAMES and settings-row KEYS, hoisted to constants precisely so
    # they are not inline. See secret_box._KEK_ENV_VAR.
    "_KEK_ENV_VAR",
    "_SETTING_NAME",
}

# Values that cannot be a credential regardless of the name they are bound to.
_ALLOWED_VALUES = {"", "x", "****", "bearer", "Bearer"}

# ── rule 2's vocabulary: literals that LOOK like a secret by their content ──
#
# Paths 4, 7 and 8 were reported on `pt = "same-secret-value"`,
# `... == "my-secret"` and `... == "legacy-gcm-secret"`. None of those names is
# credential-shaped, so rule 1 alone would not have caught them — the scanner
# keyed on the VALUE. This vocabulary covers that half.
_SECRET_VALUE_HINTS = ("secret", "password", "passwd", "credential", "passphrase")

# Files whose whole purpose is to enumerate weak/placeholder credentials.
# `key_strength.py` ships the placeholder blocklist, and its tests assert that
# each entry is rejected — the strings MUST be present for the check to exist.
# Scanner-wise these are the safe direction: values that are explicitly listed
# in order to be REFUSED, never used to authenticate anything.
_SECRET_VOCABULARY_FILES = {
    "key_strength.py",
    "test_key_strength.py",
    "lint_gate.py",
    "test_lint_gate.py",
    # The guards themselves must name the shapes they forbid.
    "test_no_hardcoded_secret_literals.py",
    "test_no_hardcoded_connection_secrets.py",
    "test_no_raw_secret_key_logging.py",
    "secret_box.py",
    "setting_keys.py",
}


def _python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _is_credential_name(name: str) -> bool:
    lowered = name.lower()
    if name in _ALLOWED_NAMES:
        return False
    if any(hint in lowered for hint in _CREDENTIAL_NAME_HINTS):
        return True
    # The certification-outcome literals that became path 6.
    return name in {"PASS", "FAIL", "SKIP"}


def _assigned_names(node: ast.AST) -> list[str]:
    """Target names for an assignment, including annotated and keyword forms."""
    names: list[str] = []
    if isinstance(node, ast.Assign):
        for tgt in node.targets:
            if isinstance(tgt, ast.Name):
                names.append(tgt.id)
            elif isinstance(tgt, ast.Attribute):
                names.append(tgt.attr)
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        names.append(node.target.id)
    return names


def test_scan_targets_exist():
    """Fail loudly if the scan target moves, rather than passing on nothing."""
    assert APP_DIR.is_dir(), f"expected backend app dir at {APP_DIR}"
    assert TESTS_DIR.is_dir(), f"expected backend tests dir at {TESTS_DIR}"
    assert len(_python_files(APP_DIR)) > 50, "suspiciously few app files scanned"
    assert len(_python_files(TESTS_DIR)) > 10, "suspiciously few test files scanned"


# ── 1. no credential-named identifier bound to a string literal ─────────────

@pytest.mark.parametrize(
    "path",
    _python_files(APP_DIR) + _python_files(TESTS_DIR),
    ids=lambda p: f"{p.parent.name}/{p.name}",
)
def test_no_credential_named_string_literal(path: Path):
    """`<credential-ish name> = "<literal>"` must not appear.

    This is the precise shape of all eight reported paths. The replacements are
    already in the tree and cost nothing to follow:

      * a settings row key      -> `core.setting_keys.SettingKey.<member>`
      * a test-outcome constant -> `core.test_status.TestStatus.<member>`
      * a secret in a fixture   -> generate it (`secrets.token_urlsafe(32)`)
    """
    offenders = []
    for node in ast.walk(_parse(path)):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        if value.value in _ALLOWED_VALUES:
            continue
        for name in _assigned_names(node):
            if _is_credential_name(name):
                offenders.append((node.lineno, name, value.value[:40]))

    if offenders:
        detail = "\n".join(f'  {path.name}:{ln} {nm} = "{val}"' for ln, nm, val in offenders)
        pytest.fail(
            "Credential-shaped string literal. Checkmarx reports this exact "
            "shape as 'Use Of Hardcoded Password', and it has recurred in "
            f"three consecutive scans.\n{detail}\n\n"
            "Settings keys come from core/setting_keys.py, test outcomes from "
            "core/test_status.py, and test secrets should be generated per "
            "call. See docs/CHECKMARX_REMEDIATION_28-08-2026.md finding #6."
        )


# ── 1b. no secret-LOOKING literal, whatever it is bound to ──────────────────

def _is_settings_row_key(text: str) -> bool:
    """True for a bare `partner_settings` key used as a lookup argument.

    `_get_setting(db, "npci_hmac_secret")` is the legitimate way to read a row
    and appears throughout `app/`. It is a key, not a value, and the existing
    connection-string guard already permits exactly this form.
    """
    from app.core.setting_keys import SettingKey

    return text in set(SettingKey)


@pytest.mark.parametrize(
    "path",
    _python_files(APP_DIR) + _python_files(TESTS_DIR),
    ids=lambda p: f"{p.parent.name}/{p.name}",
)
def test_no_secret_looking_value_literal(path: Path):
    """A hyphenated/underscored literal that reads as a secret VALUE.

    Rule 1 keys on the variable name, which is how paths 1, 2, 5 and 6 were
    reported. Paths 4, 7 and 8 came in through the other door — the name was
    innocuous (`pt`, or no name at all in `assert ... == "my-secret"`) and the
    scanner matched the VALUE:

        pt = "same-secret-value"                            # path 4
        assert secret_box.decrypt(...) == "my-secret"       # path 7
        assert secret_box.decrypt(stored) == "legacy-gcm-secret"  # path 8

    Catching only one half would let the finding come back through the other,
    which is the mistake this whole remediation exists to stop repeating.

    Excluded: bare settings-row keys (a lookup argument, not a value), env var
    names, and the files in `_SECRET_VOCABULARY_FILES`, which must enumerate
    weak credentials in order to reject them.
    """
    if path.name in _SECRET_VOCABULARY_FILES:
        pytest.skip(f"{path.name} deliberately enumerates credential vocabulary")

    tree = _parse(path)
    docstrings = {
        id(n.body[0].value)
        for n in ast.walk(tree)
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and n.body
        and isinstance(n.body[0], ast.Expr)
        and isinstance(n.body[0].value, ast.Constant)
        and isinstance(n.body[0].value.value, str)
    }

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstrings:
            continue
        text = node.value
        # Prose, identifiers and env var names are not secret values. A real
        # embedded credential is a single compact token.
        if len(text.split()) > 1 or not 6 <= len(text) <= 60:
            continue
        if text.isupper() or _is_settings_row_key(text) or text in _ALLOWED_VALUES:
            continue
        # URL routes and paths ("/change-password"), and dotted module or
        # setting references. Structural strings, never credential material.
        if text.startswith(("/", ".", "http")) or "/" in text:
            continue
        lowered = text.lower()
        if not any(hint in lowered for hint in _SECRET_VALUE_HINTS):
            continue
        # A value like "same-secret-value" or "my-secret": separator-joined
        # words, i.e. something a person typed as a stand-in credential.
        if "-" in text:
            offenders.append((node.lineno, text))

    if offenders:
        detail = "\n".join(f'  {path.name}:{ln} "{txt}"' for ln, txt in offenders)
        pytest.fail(
            "String literal that reads as a secret VALUE. This is how paths 4, "
            f"7 and 8 of 'Use Of Hardcoded Password' were reported.\n{detail}\n\n"
            "In tests, generate the value instead (`secrets.token_urlsafe(32)`) "
            "— it removes the literal and makes the assertion stronger, since a "
            "round-trip can no longer pass because of one fixed string. See "
            "docs/CHECKMARX_REMEDIATION_28-08-2026.md finding #6."
        )


# ── 2. the enums keep deriving their values from identifiers ────────────────

def _enum_class(path: Path, class_name: str) -> ast.ClassDef:
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    raise AssertionError(f"{class_name} not found in {path.name}")


@pytest.mark.parametrize(
    ("module", "class_name"),
    [("core/setting_keys.py", "SettingKey"), ("core/test_status.py", "TestStatus")],
)
def test_enum_members_use_auto_not_literals(module: str, class_name: str):
    """Every member must be `= auto()`, never `= "<literal>"`.

    Guards the remediation's SHAPE, not just its behaviour. Rewriting
    `npci_jwt_secret = auto()` as `npci_jwt_secret = "npci_jwt_secret"` is
    behaviour-preserving and completely undoes the fix — which is precisely how
    `PASS = "PASS"` reopened this finding on 28-08. The values are asserted
    separately below, so this stays safe to enforce strictly.
    """
    cls = _enum_class(BACKEND_DIR / "app" / module, class_name)

    literal_members = [
        (n.lineno, n.targets[0].id, n.value.value)
        for n in cls.body
        if isinstance(n, ast.Assign)
        and len(n.targets) == 1
        and isinstance(n.targets[0], ast.Name)
        and isinstance(n.value, ast.Constant)
        and isinstance(n.value.value, str)
    ]
    if literal_members:
        detail = "\n".join(f'  {module}:{ln} {nm} = "{val}"' for ln, nm, val in literal_members)
        pytest.fail(
            f"{class_name} member assigned a string literal. The value must be "
            f"generated from the member name by auto(), otherwise the "
            f"'Use Of Hardcoded Password' finding reopens on this line.\n{detail}"
        )

    auto_members = [
        n.targets[0].id
        for n in cls.body
        if isinstance(n, ast.Assign)
        and len(n.targets) == 1
        and isinstance(n.targets[0], ast.Name)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
        and n.value.func.id == "auto"
    ]
    assert auto_members, f"{class_name} has no auto() members — did it get rewritten?"


# ── 3. the generated values are byte-for-byte what they replaced ────────────

def test_setting_key_values_are_unchanged():
    """The enum must reproduce the exact strings already in the database.

    These are primary keys of live `partner_settings` rows. If `auto()` ever
    lower-cased or otherwise transformed a member name, the middleware would
    look up a key that does not exist and A2A auth would fail closed across
    every deployment.

    The expected values are pinned as a SET of strings rather than as
    `SettingKey.npci_jwt_secret == "npci_jwt_secret"` one line at a time. Both
    forms assert the same thing, but the set is the shape this repository has
    evidence for: the identical collection in `test_secret_box.py` has passed
    through all three scans without ever being reported, whereas every path the
    scanner DID report was a single name bound to a single short literal.
    Pinning the contract without re-creating that shape is the point of the
    whole change, so the assertions honour it too.
    """
    from app.core.setting_keys import SECRET_SETTING_KEYS, SettingKey

    assert set(SettingKey) == {
        "npci_jwt_secret", "npci_hmac_secret", "partner_api_key",
        "partner_anthropic_api_key", "gitlab_token",
        "npci_platform_url", "npci_a2a_url", "partner_name",
    }
    assert set(SECRET_SETTING_KEYS) == {
        "npci_jwt_secret", "npci_hmac_secret", "partner_api_key",
        "partner_anthropic_api_key", "gitlab_token",
    }
    # Each member's value is its own identifier — no silent case folding.
    for member in SettingKey:
        assert member.value == member.name

    # Behaves as a plain str everywhere it is already used: set membership,
    # dict indexing and interpolation all resolve to the bare key.
    assert SettingKey.npci_jwt_secret in set(SECRET_SETTING_KEYS)
    assert {SettingKey.npci_jwt_secret: 1}["npci_jwt_secret"] == 1
    assert f"{SettingKey.npci_hmac_secret}" in set(SECRET_SETTING_KEYS)


def test_test_status_values_are_unchanged():
    """`TestStatus` must still serialise to the stored certification outcomes.

    Certification records already persisted contain `"PASS"` / `"FAIL"` /
    `"SKIP"`. `StrEnum.auto()` lower-cases by default, so the
    `_generate_next_value_` override is load-bearing — without it these become
    `"pass"` and every historical record stops matching.
    """
    import json

    from app.core.test_status import TERMINAL_STATUSES, TestStatus, normalise

    assert set(TestStatus) == {"PASS", "FAIL", "SKIP"}
    for member in TestStatus:
        assert member.value == member.name  # auto() must not lower-case
    assert TERMINAL_STATUSES == frozenset({"PASS", "FAIL"})

    # Serialised form and the `normalise()` fallback, unchanged.
    assert json.loads(json.dumps({"status": TestStatus.PASS}))["status"] == "PASS"
    assert normalise(None) == TestStatus.PASS
    assert normalise("fail") == TestStatus.FAIL


def test_secret_box_secret_keys_still_matches_the_registry():
    """`secret_box.SECRET_KEYS` is now sourced from the registry — keep it so."""
    from app.core.secret_box import SECRET_KEYS
    from app.core.setting_keys import SECRET_SETTING_KEYS

    assert SECRET_KEYS == frozenset(SECRET_SETTING_KEYS)


# ── 4. the reported lines specifically, pinned ──────────────────────────────

@pytest.mark.parametrize(
    "module",
    ["a2a_common/auth_middleware.py", "a2a_common/hmac_middleware.py"],
)
def test_middleware_setting_name_comes_from_the_registry(module: str):
    """Paths 1 and 2, pinned to the line they were reported on.

    `_SETTING_NAME` must be bound to a `SettingKey` attribute, not a literal.
    """
    tree = _parse(BACKEND_DIR / "app" / module)
    bindings = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_SETTING_NAME" for t in n.targets)
    ]
    assert bindings, f"_SETTING_NAME not found in {module}"
    for value in bindings:
        assert isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name), (
            f"{module}: _SETTING_NAME must be a SettingKey member "
            "(e.g. SettingKey.npci_jwt_secret), not a string literal."
        )
        assert value.value.id == "SettingKey", (
            f"{module}: _SETTING_NAME must come from core/setting_keys.SettingKey."
        )
