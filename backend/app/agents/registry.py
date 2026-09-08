# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Agent registry — the orchestrator's single lookup point.

`registry.get("feasibility")` returns the configured `Agent` (in-process or
remote) for that name. The registry is built lazily from `config/agents.yaml`
on first access (see `loader.build_registry`). Handlers and API endpoints
call `registry.get(name).execute(...)` and never import concrete agents.
"""
from __future__ import annotations

import logging
import threading

from app.agents.base import Agent

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_REGISTRY: dict[str, Agent] = {}
_built = False


def register(agent: Agent) -> None:
    _REGISTRY[agent.name] = agent


def clear() -> None:
    """Reset the registry (used by `build_registry` and tests)."""
    global _built
    _REGISTRY.clear()
    _built = False


def _ensure_built() -> None:
    global _built
    if _built:
        return
    with _lock:
        if _built:
            return
        # Imported here to avoid an import cycle (loader imports registry).
        from app.agents.loader import build_registry
        build_registry()
        _built = True


def rebuild() -> None:
    """Force a rebuild from the manifest (e.g. after a config change/tests)."""
    from app.agents.loader import build_registry
    with _lock:
        build_registry()
        global _built
        _built = True


def get(name: str) -> Agent:
    _ensure_built()
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"no agent registered under {name!r}; configured: {sorted(_REGISTRY)}"
        ) from None


def has(name: str) -> bool:
    _ensure_built()
    return name in _REGISTRY


def all_names() -> list[str]:
    _ensure_built()
    return sorted(_REGISTRY)
