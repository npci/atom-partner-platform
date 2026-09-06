# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""`Agent` — the base interface every partner agent implements.

This is the **plug-in seam**. The platform never calls an agent's logic
directly; it calls `execute()`, which owns the lifecycle + `agent_runs`
audit and delegates the actual work to `run()`. Partners override `run()`
(and optionally the lifecycle hooks) — they do NOT touch `execute()` or the
audit wiring. Remote agents subclass this too (see `remote.RemoteAgent`),
so an HTTP-hosted agent gets identical audit/lifecycle for free.

    class MyAgent(Agent):
        def run(self, input: dict) -> dict:
            return {"status": "ok", ...}

Wire it in `config/agents.yaml`:
    agents:
      feasibility: { impl: app.agents.feasibility:FeasibilityAgent, prompt: feasibility.md }
"""
from __future__ import annotations

import abc

from sqlalchemy.orm import Session

from app.agents import audit


class Agent(abc.ABC):
    """Base agent. Subclasses implement `run(input) -> output`.

    Class attrs `mode`/`endpoint` describe how the agent is reached and are
    recorded on each `agent_runs` row. In-process agents keep the defaults;
    `RemoteAgent` overrides them.
    """

    mode: str = "local"
    endpoint: str | None = None

    def __init__(
        self,
        name: str,
        *,
        prompt: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        **opts,
    ) -> None:
        self.name = name
        self.prompt = prompt          # prompt filename in app/agents/prompts/
        self.model = model            # per-agent model override (else global config)
        self.provider = provider      # per-agent provider override (else global config)
        self.opts = opts
        # RemoteAgent sets this so audit can capture the HTTP status.
        self._last_http_status: int | None = None

    def execute(
        self,
        input: dict,
        *,
        db: Session,
        change_id: str | None = None,
        user_id: str | None = None,
    ) -> dict:
        """Run the agent with full audit + lifecycle. THIS is the call site
        the orchestrator uses — never `run()` directly."""
        row = audit.audit_start(
            db,
            agent_name=self.name,
            mode=self.mode,
            endpoint=self.endpoint,
            input=input,
            change_id=change_id,
            user_id=user_id,
        )
        self.before_run(input)
        try:
            output = self.run(input)
            self.after_run(input, output)
            audit.audit_succeed(db, row, output, http_status=self._last_http_status)
            return output
        except Exception as exc:  # noqa: BLE001 — recorded then re-raised
            audit.audit_fail(db, row, exc, http_status=self._last_http_status)
            raise

    @abc.abstractmethod
    def run(self, input: dict) -> dict:
        """Do the work. Receives a plain dict, returns a plain dict.
        No DB/ORM objects cross this boundary — keeps agents decoupled and
        unit-testable, and lets a remote agent honor the same shape."""
        raise NotImplementedError

    # ── optional lifecycle hooks (no-ops by default) ───────────────────────
    def before_run(self, input: dict) -> None:  # noqa: D401
        """Hook fired before `run()`."""

    def after_run(self, input: dict, output: dict) -> None:  # noqa: D401
        """Hook fired after a successful `run()`."""
