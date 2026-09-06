# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""`RemoteAgent` — calls a bank-hosted agent over HTTP (the `url:` binding).

Subclasses `Agent`, so audit + lifecycle are identical to in-process agents.
A bank flips an agent from shipped in-process code to their own service by
changing one manifest line:

    agents:
      code: { url: https://bank-host/agents/code, auth: bearer, timeout_s: 30, retries: 2 }

Wire contract (see ARCHITECTURE.md):
    POST {url}/run
      Authorization: Bearer <token from token_env>   (when auth: bearer)
      X-Correlation-Id: <uuid>
      body: {"agent": <name>, "input": {...}, "change_id": <id|null>, "metadata": {}}
    200 -> {"status": "ok", "output": {...}}   (or a bare output dict)
    4xx/5xx or {"status": "error", "error": "..."} -> raised, recorded as a failed run
"""
from __future__ import annotations

import logging
import os
import uuid

import httpx

from app.agents.base import Agent

logger = logging.getLogger(__name__)


class RemoteAgent(Agent):
    mode = "http"

    def __init__(
        self,
        name: str,
        *,
        url: str,
        auth: str = "bearer",
        timeout_s: float = 30.0,
        retries: int = 2,
        token_env: str = "AGENT_SERVICE_TOKEN",
        **opts,
    ) -> None:
        super().__init__(name, **opts)
        self.endpoint = url.rstrip("/")
        self.auth = auth
        self.timeout_s = timeout_s
        self.retries = max(0, int(retries))
        self.token_env = token_env

    def run(self, input: dict) -> dict:
        headers = {
            "Content-Type": "application/json",
            "X-Correlation-Id": uuid.uuid4().hex,
        }
        if self.auth == "bearer":
            token = os.getenv(self.token_env, "")
            if token:
                headers["Authorization"] = f"Bearer {token}"
            else:
                logger.warning(
                    "RemoteAgent %s: auth=bearer but %s is unset — calling unauthenticated",
                    self.name, self.token_env,
                )
        change_id = input.get("change_id") if isinstance(input, dict) else None
        body = {"agent": self.name, "input": input, "change_id": change_id, "metadata": {}}

        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            self._last_http_status = None
            try:
                resp = httpx.post(
                    f"{self.endpoint}/run", json=body, headers=headers, timeout=self.timeout_s
                )
                self._last_http_status = resp.status_code
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, dict) and data.get("status") == "error":
                    raise RuntimeError(f"remote agent error: {data.get('error')}")
                # Accept {"status":"ok","output":{...}} or a bare output dict.
                if isinstance(data, dict) and "output" in data:
                    return data["output"]
                return data
            except Exception as exc:  # noqa: BLE001 — retried, then re-raised
                last_exc = exc
                logger.warning("RemoteAgent %s attempt %d/%d failed", self.name, attempt + 1, self.retries + 1, exc_info=True)
        assert last_exc is not None
        raise last_exc
