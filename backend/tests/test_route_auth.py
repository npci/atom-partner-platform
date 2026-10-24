"""Operator routes need a session; A2A middlewares fail closed in production."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.a2a_common.auth_middleware import PartnerAuthMiddleware
from app.a2a_common.hmac_middleware import PartnerHmacMiddleware
from app.config import settings
from app.main import app

PROTECTED_PREFIXES = ("/api/cert-flow", "/api/feasibility")


def _protected_routes():
    return [r for r in app.routes
            if getattr(r, "path", "").startswith(PROTECTED_PREFIXES)]


def test_the_protected_prefixes_are_not_empty():
    assert len(_protected_routes()) >= 7


@pytest.mark.parametrize("route", _protected_routes(), ids=lambda r: f"{sorted(r.methods)} {r.path}")
def test_operator_routes_reject_an_anonymous_caller(route):
    method = sorted(route.methods - {"HEAD"})[0]
    path = route.path.replace("{change_id}", "x")
    resp = TestClient(app).request(method, path, json={})
    assert resp.status_code == 401, resp.text


def _a2a_app(middleware):
    inner = FastAPI(routes=[Route("/rpc", lambda r: PlainTextResponse("reached"), methods=["POST"])])
    inner.add_middleware(middleware)
    return TestClient(inner)


@pytest.mark.parametrize("middleware", [PartnerAuthMiddleware, PartnerHmacMiddleware])
def test_a2a_middleware_refuses_in_production_without_a_secret(monkeypatch, middleware):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(middleware, "_load_secret", lambda self: None)
    resp = _a2a_app(middleware).post("/rpc", json={})
    assert resp.status_code == 401
    assert resp.json()["error"] == "a2a_secret_not_configured"


@pytest.mark.parametrize("middleware", [PartnerAuthMiddleware, PartnerHmacMiddleware])
def test_a2a_middleware_still_passes_in_dev_without_a_secret(monkeypatch, middleware):
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(middleware, "_load_secret", lambda self: None)
    resp = _a2a_app(middleware).post("/rpc", json={})
    assert resp.status_code == 200 and resp.text == "reached"
