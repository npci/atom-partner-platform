# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Build the agent registry from the YAML manifest (`config/agents.yaml`).

Per-agent the manifest declares exactly one binding:
  - `impl: module:Class`  → imported and run in-process (default; shipped code)
  - `url: https://...`    → wrapped in `RemoteAgent` (bank-hosted HTTP service)
  - `mcp: {...}`          → DESIGNED-FOR, NOT BUILT this pass: raises a clear error
                            so the seam is reserved without a silent no-op.

Plus optional `prompt`, `provider`, `model`, `enabled` per agent. Secrets are
never here — keys live in env / the partner_settings DB (see core/llm.py).
"""
from __future__ import annotations

import importlib
import logging
import os
from pathlib import Path

import yaml

from app.agents import registry
from app.agents.remote import RemoteAgent
from app.config import settings

logger = logging.getLogger(__name__)


def _config_path() -> Path:
    configured = os.getenv("AGENTS_CONFIG") or settings.agents_config or ""
    if configured:
        return Path(configured)
    # backend/app/agents/loader.py → parents[2] == backend/
    return Path(__file__).resolve().parents[2] / "config" / "agents.yaml"


def build_registry() -> None:
    """(Re)populate the registry from the manifest. Clears first so it's idempotent."""
    registry.clear()
    path = _config_path()
    if not path.exists():
        logger.warning("agents manifest not found at %s — no agents registered", path)
        return
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    agents = data.get("agents") or {}
    for name, spec in agents.items():
        if not isinstance(spec, dict) or spec.get("enabled") is False:
            logger.info("agent %r disabled or malformed — skipping", name)
            continue
        agent = _build_agent(name, spec)
        registry.register(agent)
        logger.info("registered agent %r (mode=%s)", name, agent.mode)


def _build_agent(name: str, spec: dict):
    common = {
        "prompt": spec.get("prompt"),
        "model": spec.get("model"),
        "provider": spec.get("provider"),
    }
    if "impl" in spec:
        module_path, _, cls_name = str(spec["impl"]).partition(":")
        if not cls_name:
            raise ValueError(f"agent {name!r}: impl must be 'module:Class', got {spec['impl']!r}")
        module = importlib.import_module(module_path)
        cls = getattr(module, cls_name)
        return cls(name=name, **common)
    if "url" in spec:
        return RemoteAgent(
            name=name,
            url=spec["url"],
            auth=spec.get("auth", "bearer"),
            timeout_s=spec.get("timeout_s", 30),
            retries=spec.get("retries", 2),
            token_env=spec.get("token_env", "AGENT_SERVICE_TOKEN"),
            **common,
        )
    if "mcp" in spec:
        raise NotImplementedError(
            f"agent {name!r}: the 'mcp:' binding is designed-for but not built this pass. "
            "Use 'impl:' (in-process) or 'url:' (HTTP), or add the McpAgent adapter."
        )
    raise ValueError(f"agent {name!r}: spec must declare one of impl:/url:/mcp:")
