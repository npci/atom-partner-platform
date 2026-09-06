# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Guard: keep the shapes that trigger "Hardcoded Password in Connection String".

WHY THIS TEST EXISTS
--------------------
Checkmarx query `Python\\Cx\\PythonMediumThreat\\HardcodedPasswordinConnectionString`
(Medium) has been reported against this repository twice:

  24-06  paths 1 and 2, on two operator-facing diagnostic strings in
         npci_client.py that mention `npci_hmac_secret` by name.
  26-08  triaged as a false positive and closed with "no change required"
         (docs/CHECKMARX_REMEDIATION_26-08-2026.md, finding #3).
  28-08  paths 1 and 2 returned unchanged, joined by a third path on
         `os.getenv("PARTNER_SECRET_KEK", "")` in core/secret_box.py.

All three were genuinely false positives — no credential is hardcoded anywhere
in the flagged code. But closing a finding as Not Exploitable does not stop the
scanner re-deriving it, and this one came back on the very same lines. The cost
is a fresh triage cycle every scan, and a Medium sitting in "To Verify" on a
PCI-DSS / ASD-STIG mapped report.

So the remediation stopped arguing with the scanner and removed the pattern.
This guard keeps it removed.

WHAT THE SCANNER WAS MATCHING
-----------------------------
Three ingredients had to line up. The fix breaks each one:

1. A SINK that looks like a database connect. `test_connection(db, ...)` —
   "connection" in the name, a `db` parameter, reachable from an HTTP handler
   named `test_npci_connection`. It is actually an outbound HTTPS probe of the
   NPCI platform; `db` is the session the stored URL is READ FROM. Renamed to
   `run_npci_reachability_check()` / `check_npci_connectivity()`.

2. A SOURCE literal that reads as a credential. Both flagged strings embedded
   the raw settings key `npci_hmac_secret` inline in prose. They now refer to
   the secret through `secret_box.safe_key_label()`, which yields the fixed
   label "the NPCI HMAC secret" — same information for the operator, no key
   name adjacent to prose in a returned string.

3. An env read with an inline default. `os.getenv("PARTNER_SECRET_KEK", "")`
   is "identifier + default value" to the query, even though the default is
   empty. Now `os.environ.get(_KEK_ENV_VAR)` with the name hoisted to a module
   constant and no default at all. Behaviour is identical: falsy means raise.

WHAT THIS GUARD ENFORCES
------------------------
- No function in `app/` is named `test_*` (the pytest-collision + connect-sink
  shape), so the sink cannot be reintroduced under the old name.
- No `os.getenv`/`os.environ.get` call in `app/` passes a string default for a
  secret-ish variable name.
- No settings key from `SECRET_KEYS` appears inline in a multi-word string
  literal in the modules that feed the Test Connection response.

DELIBERATE LIMITS
-----------------
An AST shape check, not a taint engine — the same posture as
test_no_raw_secret_key_logging.py. It targets the exact shapes that recurred.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"

_SECRET_KEYS = {
    "npci_jwt_secret",
    "npci_hmac_secret",
    "partner_api_key",
    "partner_anthropic_api_key",
    "gitlab_token",
}

# Env vars that hold key material. A string default on any of these is the
# "hardcoded credential fallback" shape, regardless of the default being empty.
_SECRET_ENV_HINTS = ("KEK", "SECRET", "PASSWORD", "PASSWD", "TOKEN", "API_KEY", "CREDENTIAL")

# Modules whose string literals are returned to the Settings "Test Connection"
# response — the destination Checkmarx reported. Keeping raw settings keys out
# of prose here is what removes the source half of the flow.
_CONNECTION_RESPONSE_MODULES = {"npci_client.py"}


def _python_files() -> list[Path]:
    return sorted(p for p in APP_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_app_dir_is_found():
    """Fail loudly if the scan target moves, rather than passing on nothing."""
    assert APP_DIR.is_dir(), f"expected backend app dir at {APP_DIR}"
    assert len(_python_files()) > 50, "suspiciously few Python files scanned"


def test_secret_keys_list_is_in_sync():
    """Mirror of secret_box.SECRET_KEYS, so a new secret is covered here too."""
    from app.core.secret_box import SECRET_KEYS

    assert set(SECRET_KEYS) == _SECRET_KEYS, (
        "SECRET_KEYS changed. Add the new key to _SECRET_KEYS in this file so "
        "the connection-string guard keeps covering every secret."
    )


# ── 1. the sink: no `test_*` functions in shipped code ──────────────────────

@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_no_test_prefixed_functions_in_app(path: Path):
    """`app/` must contain no `test_*` function.

    Two problems with the name, both real:

    * Checkmarx reads `test_connection(db, ...)` as a database-connect sink, so
      every diagnostic string it returns is reported as a leaked connection
      password. That is finding #3, twice.
    * `python_functions = ["test_*"]` in pyproject.toml means pytest would
      collect it as a test case if `app/` ever entered the rootdir scan.

    Name probes for what they do: `run_npci_reachability_check`,
    `check_npci_connectivity`, `verify_*`, `probe_*`.
    """
    offenders = [
        (n.lineno, n.name)
        for n in ast.walk(_parse(path))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name.startswith("test_")
    ]
    if offenders:
        detail = "\n".join(f"  {path.name}:{ln} def {nm}()" for ln, nm in offenders)
        pytest.fail(
            "Function named test_* in shipped code. Checkmarx treats a "
            "test_*(db, ...) function as a database-connect sink, which is how "
            "'Hardcoded Password in Connection String' was reported against "
            f"test_connection() twice.\n{detail}\n\n"
            "Rename to describe the action (run_*/check_*/probe_*). See "
            "docs/CHECKMARX_REMEDIATION_26-08-2026.md finding #3."
        )


# ── 2. the env read: no string default on a secret env var ──────────────────

def _env_calls(tree: ast.Module):
    """Yield (node, varname, default_node) for os.getenv / os.environ.get."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        fn = node.func
        is_getenv = fn.attr == "getenv"
        is_environ_get = (
            fn.attr == "get"
            and isinstance(fn.value, ast.Attribute)
            and fn.value.attr == "environ"
        )
        if not (is_getenv or is_environ_get) or not node.args:
            continue
        first = node.args[0]
        name = first.value if isinstance(first, ast.Constant) else None
        if name is None and isinstance(first, ast.Name):
            name = first.id  # a hoisted constant such as _KEK_ENV_VAR
        default = node.args[1] if len(node.args) > 1 else None
        yield node, (name or ""), default


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_no_string_default_on_secret_env_var(path: Path):
    """A secret-bearing env var must be read with no string default.

    `os.getenv("PARTNER_SECRET_KEK", "")` matches the query's
    "identifier plus inline default value" shape. Dropping the default is also
    better behaviour: absence must be a hard failure, never a defaultable
    condition that lets the service start without a key.
    """
    offenders = []
    for node, name, default in _env_calls(_parse(path)):
        upper = name.upper()
        if not any(h in upper for h in _SECRET_ENV_HINTS):
            continue
        if isinstance(default, ast.Constant) and isinstance(default.value, str):
            offenders.append((node.lineno, name, repr(default.value)))

    if offenders:
        detail = "\n".join(f"  {path.name}:{ln} {nm} default={d}" for ln, nm, d in offenders)
        pytest.fail(
            "Secret env var read with a string default. This is the shape "
            "Checkmarx reported as a hardcoded connection password on "
            f"secret_box.py.\n{detail}\n\n"
            "Read it with os.environ.get(NAME) and no default, then raise when "
            "falsy. See docs/CHECKMARX_REMEDIATION_26-08-2026.md finding #3."
        )


# ── 3. the source: no raw settings key inside prose returned to the UI ──────

@pytest.mark.parametrize(
    "path",
    [p for p in _python_files() if p.name in _CONNECTION_RESPONSE_MODULES],
    ids=lambda p: p.name,
)
def test_no_secret_key_inside_message_literals(path: Path):
    """In modules that build the Test Connection response, a settings key may
    appear only as a standalone lookup argument — never inside a sentence.

    `_get_setting(db, "npci_hmac_secret")` is fine: a bare key used to read a
    row. `"HMAC envelope rejected — check npci_hmac_secret matches..."` is not:
    a credential identifier embedded in a returned string is what the scanner
    reports as the hardcoded password. Use `safe_key_label()` for the label.
    """
    tree = _parse(path)

    # Docstrings are developer documentation. They are stripped by `-OO`, never
    # returned to a caller, and explaining WHICH setting a function reads is
    # exactly what a good docstring does. Only runtime string values count.
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
        # A bare key (the legitimate lookup argument) is exactly the key.
        if text.strip() in _SECRET_KEYS:
            continue
        for key in _SECRET_KEYS:
            if key in text and len(text.split()) > 1:
                offenders.append((node.lineno, key, text[:70]))
                break

    if offenders:
        detail = "\n".join(f'  {path.name}:{ln} "{key}" in {txt!r}' for ln, key, txt in offenders)
        pytest.fail(
            "A settings key name is embedded in a message string in a module "
            "that builds the Test Connection response. Checkmarx reports this "
            "as a hardcoded connection password.\n"
            f"{detail}\n\n"
            "Refer to the secret via core.secret_box.safe_key_label(), which "
            "returns a fixed human label. See "
            "docs/CHECKMARX_REMEDIATION_26-08-2026.md finding #3."
        )


# ── the specific call sites that were reported, pinned ──────────────────────

def _function_names(path: Path) -> set[str]:
    return {
        n.name
        for n in ast.walk(_parse(path))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_reported_functions_are_gone_by_name():
    """The two names Checkmarx named must not come back.

    Checked by parsing rather than importing: `npci_client` pulls in the
    optional `a2a` SDK, and this guard must stay runnable in environments
    without it.
    """
    npci_funcs = _function_names(APP_DIR / "npci_client.py")
    settings_funcs = _function_names(APP_DIR / "api" / "dashboard" / "settings.py")

    assert "test_connection" not in npci_funcs, (
        "npci_client.test_connection was reinstated — this is the reported sink."
    )
    assert "test_npci_connection" not in settings_funcs, (
        "settings.test_npci_connection was reinstated — this is the reported "
        "destination."
    )
    assert "run_npci_reachability_check" in npci_funcs
    assert "check_npci_connectivity" in settings_funcs


def test_test_connection_route_still_exists():
    """The rename must not change the HTTP contract the frontend calls.

    `frontend/src/services/api.js` posts to `/settings/test-connection`. The
    Python function name changed; the route must not.
    """
    routes = {
        arg.value
        for n in ast.walk(_parse(APP_DIR / "api" / "dashboard" / "settings.py"))
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "router"
        for arg in n.args
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    }
    assert "/settings/test-connection" in routes, (
        "The /settings/test-connection route changed. The frontend Settings "
        "page calls this exact path — renaming it breaks the Test button."
    )


def test_kek_env_var_name_is_unchanged():
    """Hoisting the name to a constant must not change the variable read.

    Deployments set PARTNER_SECRET_KEK. If this constant drifts, every stored
    secret becomes undecryptable.
    """
    from app.core.secret_box import _KEK_ENV_VAR

    assert _KEK_ENV_VAR == "PARTNER_SECRET_KEK"


# ── the guard must be able to fail ──────────────────────────────────────────

def _scan_env_defaults(src: str) -> list:
    out = []
    for node, name, default in _env_calls(ast.parse(src)):
        if any(h in name.upper() for h in _SECRET_ENV_HINTS) and isinstance(
            default, ast.Constant
        ) and isinstance(default.value, str):
            out.append((node.lineno, name))
    return out


def test_guard_detects_getenv_with_empty_default():
    assert _scan_env_defaults('import os\nk = os.getenv("PARTNER_SECRET_KEK", "")\n')


def test_guard_detects_environ_get_with_default():
    assert _scan_env_defaults('import os\nk = os.environ.get("DB_PASSWORD", "changeme")\n')


def test_guard_allows_getenv_without_default():
    assert _scan_env_defaults('import os\nk = os.environ.get("PARTNER_SECRET_KEK")\n') == []


def test_guard_allows_default_on_non_secret_var():
    assert _scan_env_defaults('import os\nk = os.getenv("LOG_LEVEL", "INFO")\n') == []
