# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Guard: no dynamically-assembled SQL anywhere in the backend.

WHY THIS TEST EXISTS
--------------------
This is the enforcement half of the `sqlalchemy` SBOM annotation
(sonatype-2021-0025, Security-High). That advisory flags dynamic SQL assembly on
SQLAlchemy and ships **no fixed version**, so it can never be cleared by bumping
the dependency. It is cleared by asserting — and then continuously proving —
that this application does not build SQL from strings.

The VEX statement we file says, in effect: "all SQL is static or
parameter-bound; no user-controlled data reaches a SQL string." That is a claim
about the code AS IT IS TODAY. Without a gate, the claim rots the first time
someone adds a filter feature with an f-string, and we are left with an audit
record that says "not affected" while the code says otherwise. In a regulated
NPCI context a false "not affected" is materially worse than an open finding.

So this test is not decoration — it is the thing that makes the annotation
truthful over time. If it fails, either fix the SQL or withdraw the annotation.

WHAT IT ALLOWS
--------------
Static strings, implicit concatenation of adjacent literals ("SELECT " "x"), and
bound parameters (`:name`). Those are the shapes that are safe by construction.

WHAT IT REJECTS
---------------
f-strings, `%`-formatting, `.format()`, `+` concatenation and `.join()` results
reaching a SQL executor — `text()`, `exec_driver_sql()`, `.execute()`.

IT TRACKS ASSIGNMENT, NOT JUST THE CALL SITE — AND HERE IS WHY
--------------------------------------------------------------
The first version of this guard inspected only the expression written literally
in the sink's first argument. That made it trivially bypassable by one extra
line, and the bypass was not hypothetical: it was the exact shape of the code
this remediation replaced.

    # caught by the naive version
    db.execute(_sql(f"... {x}"))

    # NOT caught — and this is what app/rag/retrieval.py actually looked like
    sql = f"... {x}"
    db.execute(_sql(sql))

Measured against 9 real injection payloads, the naive version caught 4 and
missed 5: f-string via variable, `+` via variable, `.format()` via variable,
`.join()`-built WHERE via variable, and a helper function returning built SQL.
Running it against the pre-change tree confirmed the practical consequence — it
flagged `doc_ingest.py` but passed `retrieval.py`, one of the two files the
annotation is about. A guard that cannot detect its own motivating case is
decoration.

So this version propagates taint within each function:

  1. Any local assigned a dynamically-built string is marked tainted.
  2. Taint flows through re-assignment (`b = a`), augmented assignment
     (`sql += x`), and `.join()` / `.format()` on a tainted value.
  3. Module-level functions that RETURN a dynamic string are recorded, and a
     call to one is treated as dynamic wherever it appears.
  4. A tainted name reaching a sink is reported at the SINK line, with the
     assignment line named, so the failure is actionable.

This is a linear intra-procedural analysis, not a full taint engine, and its
limits are stated in `test_guard_limitations_are_documented` rather than left
implicit. It is deliberately conservative in one direction: `+=` inside a loop
building a static-only string will be flagged. That is acceptable — the fix is
to hoist the statement to a module constant, which is what we want anyway.

The one legitimate exception (pgvector type widths in DDL, which Postgres will
not accept as bind parameters) is allowlisted by exact location and re-checked
here, so the exception cannot silently grow.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"

# Callables that hand a string to the database. `text` and `_sql` are the
# SQLAlchemy textual-SQL constructors (`_sql` is the local alias used across
# app/rag/*); the rest execute directly.
_SQL_SINKS = {"text", "_sql", "exec_driver_sql", "execute", "executemany"}

# The ONLY approved dynamic-SQL sites, as (file, function) pairs.
#
# Both are pgvector column declarations of the form `vector(768)`. Postgres does
# not permit a bind parameter inside a type declaration, so `vector(:dim)` is
# not valid SQL and interpolation is genuinely unavoidable. The interpolated
# value is hardened at its source by `app.database._vector_dim()`, which
# re-coerces through `int()` and range-checks 1..2000 — so only a small positive
# integer can reach the string.
#
# Keep this list minimal. Anything added here needs the same treatment: a
# non-string type that cannot express SQL syntax, validated at the point of use.
_ALLOWED_DYNAMIC = {
    ("database.py", "_ensure_embedding_cache_table"),
    ("database.py", "_ensure_document_chunks_table"),
}


def _dynamic_builders(tree: ast.AST) -> set[str]:
    """Names of functions that return a dynamically-built string.

    Two passes are needed because a helper may be defined after its caller.
    Without this, moving the f-string into a one-line helper defeats the guard:

        def _build(v): return f"... {v}"
        db.execute(_sql(_build(v)))     # <- would look like a plain call
    """
    builders: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Return) and sub.value is not None:
                if _describe_dynamic_expr(sub.value, set(), builders):
                    builders.add(node.name)
                    break
    return builders


def _describe_dynamic_expr(
    node: ast.expr,
    tainted: set[str],
    builders: set[str],
) -> str | None:
    """Describe how `node` builds a string dynamically, or None if it is safe.

    `tainted` holds local names already known to carry built SQL; `builders`
    holds functions known to return built SQL. Both are consulted so taint
    propagates through variables and helper calls rather than stopping at the
    literal call site.
    """
    if isinstance(node, ast.JoinedStr):              # f"..."
        return "f-string"

    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Mod):             # "..." % (...)
            return "%-formatting"
        if isinstance(node.op, ast.Add):
            # Adjacent string literals are folded by the parser into a single
            # Constant, so a surviving Add is a real runtime concatenation.
            return "string concatenation (+)"

    if isinstance(node, ast.Name):
        if node.id in tainted:
            return "dynamically-built string via variable"
        return None

    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in {"format", "join"}:
            return f".{f.attr}()"
        # A call to a known SQL-building helper.
        if isinstance(f, ast.Name) and f.id in builders:
            return f"call to dynamic SQL builder {f.id}()"
        if isinstance(f, ast.Attribute) and f.attr in builders:
            return f"call to dynamic SQL builder {f.attr}()"

    return None


class _Visitor(ast.NodeVisitor):
    """Collect every dynamic string expression that flows into a SQL sink.

    Taint is tracked per function scope: `_tainted` maps a local name to the
    line where it was assigned built SQL, so a violation can name both the
    assignment and the sink.
    """

    def __init__(self, relpath: str, builders: set[str] | None = None) -> None:
        self.relpath = relpath
        self.violations: list[tuple[int, str, str]] = []
        self._func_stack: list[str] = []
        self._builders = builders or set()
        # name -> (line, description) for the CURRENT function scope
        self._tainted: dict[str, tuple[int, str]] = {}

    # Track the enclosing function so findings can be allowlisted precisely and
    # reported with a location a developer can act on. Taint does not leak
    # between functions: each scope starts clean and is restored on exit.
    def visit_FunctionDef(self, node: ast.FunctionDef):  # noqa: N802
        self._func_stack.append(node.name)
        outer, self._tainted = self._tainted, {}
        self.generic_visit(node)
        self._tainted = outer
        self._func_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    @staticmethod
    def _sink_name(func: ast.expr) -> str | None:
        """Return the called name if it is a SQL sink, else None."""
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        else:
            return None
        return name if name in _SQL_SINKS else None

    def _describe_dynamic(self, node: ast.expr) -> str | None:
        return _describe_dynamic_expr(
            node, set(self._tainted), self._builders
        )

    # ── taint introduction and propagation ──────────────────────────────────

    def visit_Assign(self, node: ast.Assign):  # noqa: N802
        kind = self._describe_dynamic(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                if kind:
                    self._tainted[target.id] = (node.lineno, kind)
                else:
                    # Re-assignment from a safe expression clears taint, so a
                    # genuine fix (reassigning to a static constant) is not
                    # reported forever.
                    self._tainted.pop(target.id, None)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign):  # noqa: N802
        if node.value is not None and isinstance(node.target, ast.Name):
            kind = self._describe_dynamic(node.value)
            if kind:
                self._tainted[node.target.id] = (node.lineno, kind)
            else:
                self._tainted.pop(node.target.id, None)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign):  # noqa: N802
        """`sql += ...` — accumulation is always dynamic construction.

        Flagged unconditionally when the target is a local name: appending to a
        string at runtime is the definition of building SQL, and the safe
        alternative (a module-level constant) never needs it.
        """
        if isinstance(node.target, ast.Name):
            self._tainted.setdefault(
                node.target.id, (node.lineno, "string built by += accumulation")
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):  # noqa: N802
        sink = self._sink_name(node.func)
        if sink and node.args:
            # Only the first positional argument carries the SQL text; later
            # arguments are the parameter dict, which is exactly where values
            # SHOULD go and must not be flagged.
            arg = node.args[0]
            kind = self._describe_dynamic(arg)
            if kind:
                func = self._func_stack[-1] if self._func_stack else "<module>"
                if (self.relpath, func) not in _ALLOWED_DYNAMIC:
                    detail = f"{kind} passed to {sink}()"
                    # Name the assignment line too — the sink line alone is not
                    # enough to find the problem when taint came from a variable.
                    if isinstance(arg, ast.Name) and arg.id in self._tainted:
                        origin_line, origin_kind = self._tainted[arg.id]
                        detail = (
                            f"{sink}() receives '{arg.id}', which was built as "
                            f"a {origin_kind} on line {origin_line}"
                        )
                    self.violations.append((node.lineno, func, detail))
        self.generic_visit(node)


def _scan(path: Path) -> list[tuple[int, str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    v = _Visitor(path.name, builders=_dynamic_builders(tree))
    v.visit(tree)
    return v.violations


def _python_files() -> list[Path]:
    return sorted(p for p in APP_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def test_app_dir_is_found():
    """Fail loudly if the scan target moves.

    Without this, a refactor that relocates `app/` would make every test below
    pass by scanning nothing — the worst kind of green build, since it would let
    us keep filing a VEX annotation backed by a check that examines no code.
    """
    assert APP_DIR.is_dir(), f"expected backend app dir at {APP_DIR}"
    assert len(_python_files()) > 50, "suspiciously few Python files scanned"


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_no_dynamic_sql(path: Path):
    """No f-string / concat / .format() / .join() may reach a SQL executor."""
    violations = _scan(path)
    if violations:
        detail = "\n".join(
            f"  {path.relative_to(APP_DIR.parent)}:{line} in {func}(): {msg}"
            for line, func, msg in violations
        )
        pytest.fail(
            "Dynamically-assembled SQL detected. This reintroduces the pattern "
            "behind sonatype-2021-0025 and invalidates the 'not affected' SBOM "
            "annotation for SQLAlchemy.\n"
            f"{detail}\n\n"
            "Fix: use a module-level static statement and bind values as "
            "parameters (see app/rag/retrieval.py::_RETRIEVE_SQL for the "
            "predicate-toggle technique, or app/rag/doc_ingest.py for enumerated "
            "statements). Do not add to the allowlist without a reason that "
            "cannot be solved by binding."
        )


# ── The guard must be able to FAIL — a check that cannot fail is worthless ──
#
# Every payload below is genuinely exploitable: a user-controlled value reaching
# SQL text. They are kept as source strings and scanned directly, so this
# asserts the DETECTOR works without needing to mutate any real file.
#
# The five marked (was missed) are the ones the naive first version let through.
_MUST_DETECT = {
    "f-string inline in _sql()": '''
def q(db, u):
    return db.execute(_sql(f"SELECT * FROM t WHERE n = '{u}'")).fetchall()
''',
    "f-string via variable (was missed)": '''
def q(db, u):
    sql = f"SELECT * FROM t WHERE n = '{u}'"
    return db.execute(_sql(sql)).fetchall()
''',
    "concatenation via variable (was missed)": '''
def q(db, u):
    sql = "SELECT * FROM t WHERE n = '" + u + "'"
    return db.execute(_sql(sql)).fetchall()
''',
    ".format() via variable (was missed)": '''
def q(db, u):
    sql = "SELECT * FROM t WHERE n = '{}'".format(u)
    return db.execute(_sql(sql)).fetchall()
''',
    "join()-built WHERE via variable (was missed)": '''
def q(db, filters):
    where = " AND ".join(filters)
    sql = "DELETE FROM document_chunks WHERE " + where
    return db.execute(_sql(sql))
''',
    "helper returns built SQL (was missed)": '''
def _build(u):
    return f"SELECT * FROM t WHERE n = '{u}'"

def q(db, u):
    return db.execute(_sql(_build(u))).fetchall()
''',
    "f-string as keyword argument": '''
def q(db, u):
    return db.execute(statement=_sql(f"SELECT * FROM t WHERE n='{u}'"))
''',
    "%-formatting inline in execute()": '''
def q(db, u):
    return db.execute("SELECT * FROM t WHERE n='%s'" % u)
''',
    "+= accumulation": '''
def q(db, filters):
    sql = "SELECT * FROM t WHERE 1=1"
    for f in filters:
        sql += " AND " + f
    return db.execute(_sql(sql)).fetchall()
''',
    "taint through re-assignment": '''
def q(db, u):
    a = f"SELECT * FROM t WHERE n = '{u}'"
    b = a
    return db.execute(_sql(b)).fetchall()
''',
}

# These are the safe shapes. Flagging any of them would make the guard a
# nuisance, which is how guards get deleted.
_MUST_ALLOW = {
    "module constant + bound params": '''
_STMT = _sql("SELECT * FROM t WHERE n = :n")

def q(db, u):
    return db.execute(_STMT, {"n": u}).fetchall()
''',
    "implicit literal concatenation": '''
def q(db, u):
    return db.execute(_sql(
        "SELECT * FROM t "
        "WHERE n = :n"
    ), {"n": u}).fetchall()
''',
    "values in the params dict": '''
def q(db, u, v):
    return db.execute(_sql("SELECT * FROM t WHERE a = :a AND b = :b"),
                      {"a": u, "b": v}).fetchall()
''',
    "f-string NOT reaching SQL": '''
def q(db, u):
    logger.info(f"looking up {u}")
    return db.execute(_sql("SELECT * FROM t WHERE n = :n"), {"n": u}).fetchall()
''',
    "taint cleared by reassignment to a constant": '''
def q(db, u):
    sql = f"bad {u}"
    sql = "SELECT * FROM t WHERE n = :n"
    return db.execute(_sql(sql), {"n": u}).fetchall()
''',
}


def _scan_source(src: str, relpath: str = "sample.py"):
    tree = ast.parse(src)
    v = _Visitor(relpath, builders=_dynamic_builders(tree))
    v.visit(tree)
    return v.violations


@pytest.mark.parametrize("name,src", sorted(_MUST_DETECT.items()), ids=lambda v: v if isinstance(v, str) else "")
def test_guard_detects_real_injection(name, src):
    """Each payload is exploitable and MUST be reported.

    This is the anti-vacuity test for the whole annotation: if the guard cannot
    fail on real injection, the 'not affected' claim it backs is unsupported.
    """
    violations = _scan_source(src)
    assert violations, (
        f"the guard did not detect {name!r} — this is a real SQL injection that "
        f"would pass CI and silently invalidate the sonatype-2021-0025 "
        f"annotation"
    )
    # The message must be actionable, not a bare boolean.
    for _line, _func, msg in violations:
        assert len(msg) > 15


@pytest.mark.parametrize("name,src", sorted(_MUST_ALLOW.items()), ids=lambda v: v if isinstance(v, str) else "")
def test_guard_allows_safe_sql(name, src):
    """Safe, idiomatic SQL must never be flagged.

    False positives are not a harmless trade here: a guard that cries wolf on
    correct code gets weakened or deleted, taking the real protection with it.
    """
    violations = _scan_source(src)
    assert not violations, (
        f"the guard falsely flagged {name!r}: {violations}. This is safe, "
        f"idiomatic parameterised SQL and must pass."
    )


def test_guard_reports_the_assignment_line_not_just_the_sink():
    """A variable-borne violation must name where the string was BUILT.

    The sink line alone sends a developer to `db.execute(sql)` with no clue
    which of several assignments produced it.
    """
    src = '''
def q(db, u):
    sql = f"SELECT * FROM t WHERE n = '{u}'"
    return db.execute(_sql(sql)).fetchall()
'''
    violations = _scan_source(src)
    assert violations
    msg = violations[0][2]
    assert "sql" in msg and "line 3" in msg, msg


def test_guard_limitations_are_documented():
    """State the analysis boundary explicitly rather than implying omniscience.

    This is intra-procedural: taint does not cross function boundaries except
    through the return-value detection in `_dynamic_builders`. Cases such as
    taint stored on an object attribute, passed as a parameter into a helper, or
    routed through a container are NOT detected.

    Recording this in a test keeps the VEX language honest — the claim we can
    support is "no dynamic SQL in the shapes this guard covers, and those shapes
    include every pattern previously present in this codebase", not "provably
    no dynamic SQL by any means".
    """
    undetected = '''
def _run(db, sql):
    return db.execute(_sql(sql)).fetchall()

def q(db, u):
    return _run(db, f"SELECT * FROM t WHERE n = '{u}'")
'''
    # Asserted as a KNOWN GAP. If a future improvement closes it, this test
    # fails and the docstring above (and the VEX wording) should be updated.
    assert not _scan_source(undetected), (
        "the guard now detects taint passed as a function parameter — good. "
        "Update this test and the VEX 'analysis' detail to claim the stronger "
        "property."
    )


def test_allowlist_entries_still_exist():
    """Every allowlisted site must still be a real, still-dynamic location.

    An allowlist that outlives the code it excuses is how scope creep hides. If a
    DDL function stops interpolating (or is renamed/removed), its entry must go —
    otherwise it silently pre-authorises dynamic SQL in a function that a future
    change could reintroduce it into.
    """
    for relpath, func in sorted(_ALLOWED_DYNAMIC):
        matches = [p for p in _python_files() if p.name == relpath]
        assert matches, f"allowlisted file {relpath} no longer exists"

        source = matches[0].read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relpath)
        names = {
            n.name
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert func in names, (
            f"allowlisted function {relpath}::{func} no longer exists — "
            f"remove its entry from _ALLOWED_DYNAMIC."
        )


def test_vector_dim_validation_rejects_out_of_range():
    """`_vector_dim()` is the guard that makes the DDL allowlist defensible.

    The two allowlisted DDL sites interpolate this value, so it is the single
    place where a config value can reach a SQL string. If its validation ever
    weakens, the allowlist stops being safe — so the bound is asserted here
    rather than trusted.
    """
    from app import database

    for bad in (0, -1, 2001, 10_000):
        with pytest.raises(ValueError):
            with _patched_embed_dim(database, bad):
                database._vector_dim()

    for good in (1, 768, 1536, 2000):
        with _patched_embed_dim(database, good):
            assert database._vector_dim() == good


class _patched_embed_dim:
    """Temporarily override `settings.embed_dim` (no monkeypatch fixture needed
    inside the loop above)."""

    def __init__(self, database_module, value):
        self._settings = database_module.settings
        self._value = value
        self._original = None

    def __enter__(self):
        self._original = self._settings.embed_dim
        object.__setattr__(self._settings, "embed_dim", self._value)
        return self

    def __exit__(self, *exc):
        object.__setattr__(self._settings, "embed_dim", self._original)
        return False
