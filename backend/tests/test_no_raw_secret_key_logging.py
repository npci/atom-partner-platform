# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Guard: no settings key or secret value may reach a logger un-wrapped.

WHY THIS TEST EXISTS
--------------------
This is the enforcement half of the Checkmarx "Filtering Sensitive Logs"
remediation (docs/CHECKMARX_REMEDIATION_26-08-2026.md, finding #4). That query
has now been reported twice against this repository:

  26-08  five paths, on log calls of the form
             logger.critical("failed to decrypt %s", key)
  28-08  six paths, after the first fix routed `key` through a constant
         lookup table that Checkmarx treats as a taint PROPAGATOR rather
         than a barrier.

Both rounds were false positives — the object logged was always the setting's
NAME ("npci_hmac_secret"), never the secret VALUE. But a finding that is
re-litigated every scan costs real review time, and the second round proved the
failure mode is subtle enough that a careful engineer got it wrong once already.

`safe_key_label()` and `hmac_middleware._safe_reason_code()` fix the sites that
exist today, and `test_safe_key_label_does_not_use_a_lookup_table` stops those
two helpers from being refactored back into the shape that failed. Neither
protects code that has not been written yet. This guard closes that gap: a NEW
log line that interpolates a settings key is caught here, in CI, instead of
three weeks later in a scan report.

WHAT IT REJECTS
---------------
A member of `SECRET_KEYS` reaching a `logger.*()` call, whether written
literally or held in a variable:

    logger.warning("rotating %s", "npci_hmac_secret")   # literal
    k = "npci_hmac_secret"; logger.warning("...", k)    # via a local
    logger.critical("failed to decrypt %s", key)        # via a key parameter

and the secret VALUE itself, read out of a settings row or request body:

    logger.info("saved %s", body.npci_jwt_secret)
    logger.info("saved %s", row.value)                  # in a secret context

WHAT IT ALLOWS
--------------
- `safe_key_label(key)` — returns a module-owned literal (the approved fix).
- `len(...)` — a length is not the secret, and the existing settings
  update-confirmation logs use it deliberately.
- Fully static messages with no interpolation at all, e.g.
  `logger.critical("_gitlab_token: failed to decrypt")` in rag/code_ingestion.py.
- The logger's format string itself, which is always a developer-written
  literal. Only the INTERPOLATED ARGUMENTS are inspected — otherwise every
  message that merely mentions a secret by name in prose would be flagged, and
  a guard that fires on correct code gets silenced rather than fixed.

DELIBERATE LIMITS
-----------------
This is an intra-procedural AST check, not a taint engine. It will not follow a
key through a helper's return value or across modules. It is aimed squarely at
the shape that actually recurred twice — a key name, in scope, handed to a
logger in the same function — and it is cheap and deterministic. Anything
subtler is the scanner's job.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"

# Kept in sync with app.core.secret_box.SECRET_KEYS by
# test_secret_keys_list_is_in_sync below, so a newly added secret is covered by
# this guard automatically rather than silently escaping it.
_SECRET_KEYS = {
    "npci_jwt_secret",
    "npci_hmac_secret",
    "partner_api_key",
    "partner_anthropic_api_key",
    "gitlab_token",
}

# Calls whose RESULT is safe to log even when a secret-bearing expression is
# passed in. `safe_key_label` maps to a module-owned literal; `len` yields an
# integer that cannot carry the secret.
_SAFE_WRAPPERS = {"safe_key_label", "len"}

_LOGGER_METHODS = {"debug", "info", "warning", "warn", "error", "critical", "exception", "log"}

# Attribute names that hold a decrypted or raw secret value.
_SECRET_VALUE_ATTRS = {"value"} | _SECRET_KEYS


def _is_logger_call(node: ast.Call) -> bool:
    """True for `logger.info(...)`, `log.warning(...)`, `self.logger.error(...)`."""
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in _LOGGER_METHODS:
        return False
    target = func.value
    while isinstance(target, ast.Attribute):
        target = target.value
    return isinstance(target, ast.Name) and "log" in target.id.lower()


class _Visitor(ast.NodeVisitor):
    """Per-function scan for secret-bearing expressions reaching a logger."""

    def __init__(self, filename: str):
        self.filename = filename
        self.violations: list[tuple[int, str, str]] = []
        self._func = "<module>"
        # Locals currently known to hold a settings key name.
        self._key_names: set[str] = set()

    # -- function scoping ---------------------------------------------------
    def visit_FunctionDef(self, node):  # noqa: N802
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node):  # noqa: N802
        self._visit_function(node)

    def _visit_function(self, node):
        prev_func, prev_keys = self._func, self._key_names
        self._func = node.name
        self._key_names = set(prev_keys)

        # A `key` parameter in a function that references SECRET_KEYS is the
        # exact shape of _get_setting()/_get() — the two functions this finding
        # was reported against.
        src_names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        if "SECRET_KEYS" in src_names:
            for arg in list(node.args.args) + list(node.args.kwonlyargs):
                if arg.arg in {"key", "setting_key", "secret_key"}:
                    self._key_names.add(arg.arg)

        self.generic_visit(node)
        self._func, self._key_names = prev_func, prev_keys

    # -- track locals holding a key name ------------------------------------
    def visit_Assign(self, node):  # noqa: N802
        if isinstance(node.value, ast.Constant) and node.value.value in _SECRET_KEYS:
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    self._key_names.add(tgt.id)
        self.generic_visit(node)

    # -- the sink -----------------------------------------------------------
    def visit_Call(self, node):  # noqa: N802
        if _is_logger_call(node):
            # Skip the format string and, for logger.log(level, msg, ...), the
            # level — but ONLY when the message is a plain literal. An f-string
            # message is interpolation wearing a format string's clothes, so it
            # gets scanned like any other argument.
            skip = 2 if (isinstance(node.func, ast.Attribute) and node.func.attr == "log") else 1
            head = [a for a in node.args[:skip] if not isinstance(a, ast.Constant)]
            for arg in head + node.args[skip:] + [kw.value for kw in node.keywords]:
                why = self._describe_secret_expr(arg)
                if why:
                    self.violations.append((node.lineno, self._func, why))
        self.generic_visit(node)

    def _describe_secret_expr(self, node: ast.expr) -> str | None:
        """Describe how `node` carries a secret, or None if it is safe."""
        # A safe wrapper neutralises everything inside it.
        if isinstance(node, ast.Call):
            fname = node.func.attr if isinstance(node.func, ast.Attribute) else (
                node.func.id if isinstance(node.func, ast.Name) else ""
            )
            if fname in _SAFE_WRAPPERS:
                return None
            for sub in node.args:
                if (why := self._describe_secret_expr(sub)):
                    return why
            return None

        if isinstance(node, ast.Constant) and node.value in _SECRET_KEYS:
            return f'the settings key "{node.value}" is written straight into the log'

        if isinstance(node, ast.Name):
            if node.id in self._key_names:
                return f"`{node.id}` holds a settings key name"
            return None

        if isinstance(node, ast.Attribute):
            if node.attr in _SECRET_VALUE_ATTRS:
                return f"`.{node.attr}` reads a secret value"
            return self._describe_secret_expr(node.value)

        if isinstance(node, ast.Subscript):
            if isinstance(node.slice, ast.Constant) and node.slice.value in _SECRET_KEYS:
                return f'a lookup keyed by "{node.slice.value}"'
            return self._describe_secret_expr(node.value)

        # f-strings and %/+ concatenation: inspect the embedded expressions.
        if isinstance(node, ast.JoinedStr):
            for v in node.values:
                if isinstance(v, ast.FormattedValue):
                    if (why := self._describe_secret_expr(v.value)):
                        return why
            return None

        if isinstance(node, ast.BinOp):
            return (self._describe_secret_expr(node.left)
                    or self._describe_secret_expr(node.right))

        return None


def _scan_source(src: str, relpath: str = "sample.py") -> list[tuple[int, str, str]]:
    v = _Visitor(relpath)
    v.visit(ast.parse(src, filename=relpath))
    return v.violations


def _python_files() -> list[Path]:
    return sorted(p for p in APP_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def test_app_dir_is_found():
    """Fail loudly if the scan target moves, rather than passing on nothing."""
    assert APP_DIR.is_dir(), f"expected backend app dir at {APP_DIR}"
    assert len(_python_files()) > 50, "suspiciously few Python files scanned"


def test_secret_keys_list_is_in_sync():
    """This guard's key list must match secret_box.SECRET_KEYS.

    If a new secret is added there but not here, it would be loggable in the
    clear without this guard noticing — the gap would open silently.
    """
    from app.core.secret_box import SECRET_KEYS

    assert set(SECRET_KEYS) == _SECRET_KEYS, (
        "SECRET_KEYS changed. Add the new key to _SECRET_KEYS in this file and "
        "give it a label in secret_box.safe_key_label()."
    )


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_no_raw_secret_key_logging(path: Path):
    """No settings key or secret value may be interpolated into a log record."""
    violations = _scan_source(path.read_text(encoding="utf-8"), path.name)
    if violations:
        detail = "\n".join(
            f"  {path.relative_to(APP_DIR.parent)}:{line} in {func}(): {why}"
            for line, func, why in violations
        )
        pytest.fail(
            "A settings key or secret value reaches a logger un-wrapped. This is "
            "the pattern behind Checkmarx 'Filtering Sensitive Logs', which has "
            "already been reported against this repo twice.\n"
            f"{detail}\n\n"
            "Fix: wrap the key with core.secret_box.safe_key_label(), which "
            "returns a fixed module-owned label, or drop the argument and log a "
            "static message. Log len(...) if you need to confirm a value was "
            "present. See docs/CHECKMARX_REMEDIATION_26-08-2026.md finding #4."
        )


# ── The guard must be able to FAIL — a check that cannot fail is worthless ──

_BAD = {
    "literal key": '''
import logging
logger = logging.getLogger(__name__)
def f():
    logger.warning("rotating %s", "npci_hmac_secret")
''',
    "key via local": '''
import logging
logger = logging.getLogger(__name__)
def f():
    k = "npci_jwt_secret"
    logger.info("touching %s", k)
''',
    "key parameter in a SECRET_KEYS function": '''
import logging
logger = logging.getLogger(__name__)
def _get_setting(db, key):
    if key not in SECRET_KEYS:
        return None
    logger.critical("failed to decrypt %s", key)
''',
    "secret value off a row": '''
import logging
logger = logging.getLogger(__name__)
def _get_setting(db, key):
    if key not in SECRET_KEYS:
        return None
    logger.info("value was %s", row.value)
''',
    "secret value off a request body": '''
import logging
logger = logging.getLogger(__name__)
def f(body):
    logger.info("saved %s", body.npci_jwt_secret)
''',
    "key inside an f-string": '''
import logging
logger = logging.getLogger(__name__)
def f():
    logger.info(f"touching {"npci_hmac_secret"}")
''',
    "dict lookup keyed by a secret": '''
import logging
logger = logging.getLogger(__name__)
def f(table):
    logger.info("label %s", table["npci_hmac_secret"])
''',
}

_GOOD = {
    "wrapped in safe_key_label": '''
import logging
logger = logging.getLogger(__name__)
def _get_setting(db, key):
    if key not in SECRET_KEYS:
        return None
    logger.critical("failed to decrypt %s", safe_key_label(key))
''',
    "length only": '''
import logging
logger = logging.getLogger(__name__)
def f(body):
    logger.info("saved (len=%d)", len(body.npci_jwt_secret))
''',
    "static message naming a secret in prose": '''
import logging
logger = logging.getLogger(__name__)
def f():
    logger.critical("_gitlab_token: failed to decrypt — treating as unconfigured")
''',
    "unrelated logging": '''
import logging
logger = logging.getLogger(__name__)
def f(user):
    logger.info("Password changed: user=%s", user.username)
''',
    "key used but not logged": '''
import logging
logger = logging.getLogger(__name__)
def _get_setting(db, key):
    if key not in SECRET_KEYS:
        return None
    return decrypt(db.get(key))
''',
}


@pytest.mark.parametrize("name,src", sorted(_BAD.items()), ids=lambda v: v if isinstance(v, str) else "")
def test_guard_detects_unsafe_logging(name, src):
    assert _scan_source(src), f"guard failed to flag: {name}"


@pytest.mark.parametrize("name,src", sorted(_GOOD.items()), ids=lambda v: v if isinstance(v, str) else "")
def test_guard_allows_safe_logging(name, src):
    assert _scan_source(src) == [], f"guard false-positived on: {name}"


def test_guard_reports_line_and_function():
    """Failures must be actionable — name the line and the function."""
    src = (
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "def handler():\n"
        '    logger.warning("rotating %s", "npci_hmac_secret")\n'
    )
    (line, func, why), = _scan_source(src)
    assert line == 4
    assert func == "handler"
    assert "npci_hmac_secret" in why
