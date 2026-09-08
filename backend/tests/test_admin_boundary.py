# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Routes that write credentials or shape the RAG corpus must require admin.

The defect this pins: `frontend/src/App.jsx` gates `/settings` and `/knowledge`
behind `AdminRoute` and comments that the backend re-enforces it — but the
code-repo and knowledge routers took `get_current_user`, not `require_admin`.
The gate was cosmetic. Any `role="user"` account could overwrite the platform's
`api`-scoped GitLab PAT with one request and let the merge-request flow push to
a repository it controlled, or poison the corpus the design/code/test agents
retrieve from.

Asserted against the DEPENDENCY TREE rather than by calling each route, so the
test cannot be satisfied by a handler that merely happens to 403 for some other
reason, and so a route added to these routers later is covered without anyone
remembering to extend a TestClient case.
"""
from fastapi.routing import APIRoute

from app.api.auth import get_current_user, require_admin
from app.main import app

# Every route whose authorisation boundary is "admin". Frozen deliberately: an
# addition here should be a decision, and a REMOVAL should have to be argued
# for in review rather than happening as a side effect of editing a signature.
ADMIN_ONLY = {
    # Credential + platform configuration
    "GET /api/settings",
    "PUT /api/settings",
    "POST /api/settings/test-connection",
    "GET /api/profile",
    "PUT /api/profile",
    "GET /api/users",
    "POST /api/users",
    "PUT /api/users/{user_id}",
    "DELETE /api/users/{user_id}",
    # Code repository — writes the GitLab PAT and drives indexing/MR creation
    "PUT /api/code-repo/token",
    "GET /api/code-repo",
    "POST /api/code-repo",
    "DELETE /api/code-repo/{repo_id}",
    "POST /api/code-repo/{repo_id}/index",
    # Knowledge base — the retrieval corpus the agents ground on.
    # `POST /api/changes/{change_id}/documents/index` lives in the same module
    # and moved with it: it drives the same ingestion path into the same
    # `document_chunks` table, so leaving it at get_current_user would have
    # kept the corpus writable by any role through a differently-named door.
    "GET /api/knowledge",
    "POST /api/knowledge",
    "DELETE /api/knowledge/{kb_id}",
    "POST /api/changes/{change_id}/documents/index",
    # Integration-testing read surfaces
    "GET /api/integration-testing/cert-executions",
    "GET /integration-testing/exchanges",
}


def _dependency_calls(dependant) -> set:
    """Every callable in a route's dependency tree, recursively."""
    found = set()
    stack = list(dependant.dependencies)
    while stack:
        dep = stack.pop()
        if dep.call is not None:
            found.add(dep.call)
        stack.extend(dep.dependencies)
    return found


def _routes() -> dict:
    """Every concrete APIRoute reachable from the app, keyed "METHOD /path".

    Walks recursively for the same reason `test_dashboard_routes._dashboard_route_set`
    does: as of FastAPI 0.141.1 `include_router()` appends one opaque
    `_IncludedRouter` wrapper per included router rather than splicing the
    child's routes into the parent list, so a flat scan of `app.routes` finds
    three health endpoints and nothing else. A flat scan here would not fail —
    it would make every assertion below vacuous, which is worse.
    """
    out: dict = {}
    seen: set[int] = set()

    def _walk(node) -> None:
        if id(node) in seen:
            return
        seen.add(id(node))
        if isinstance(node, APIRoute):
            for method in node.methods - {"HEAD", "OPTIONS"}:
                out[f"{method} {node.path}"] = node
        inner = getattr(node, "original_router", None)
        if inner is not None:
            _walk(inner)
        for child in getattr(node, "routes", []) or []:
            _walk(child)

    _walk(app.router)
    return out


def test_every_admin_only_route_exists():
    """Guards against a rename silently emptying the set below."""
    missing = ADMIN_ONLY - set(_routes())
    assert not missing, f"listed as admin-only but not registered: {sorted(missing)}"


def test_admin_only_routes_depend_on_require_admin():
    routes = _routes()
    offenders = []
    for key in sorted(ADMIN_ONLY):
        calls = _dependency_calls(routes[key].dependant)
        if require_admin not in calls:
            offenders.append(key)
    assert not offenders, (
        "these routes are admin-only but do not depend on require_admin — a "
        f"role='user' account can reach them: {offenders}"
    )


def test_admin_only_routes_do_not_settle_for_get_current_user():
    """`get_current_user` alone proves a session, not a role.

    Separate from the test above because the failure reads differently: this is
    the exact shape the defect had — a real, present, working auth dependency
    that simply was not the right one.
    """
    routes = _routes()
    offenders = []
    for key in sorted(ADMIN_ONLY):
        calls = _dependency_calls(routes[key].dependant)
        if get_current_user in calls and require_admin not in calls:
            offenders.append(key)
    assert not offenders, f"authenticated-but-any-role on admin routes: {offenders}"
