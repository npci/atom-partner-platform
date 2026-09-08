# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Backwards-compatible re-export of the shared security-event emitter.

The implementation moved to `app.core.security_events` so that non-A2A
callers (`app.core.resilience`, `app.services.outbound_retry`) can emit
security events without importing this package. Importing anything from
`app.a2a_common` executes its `__init__`, which pulls in the `a2a-sdk`
wire client — a hard dependency the LLM circuit breaker has no business
requiring. Keeping the emitter in `app.core` inverts that: the A2A layer
depends on core, never the other way round.

The A2A middlewares continue to import from here for readability; this
module is a pure alias with no behaviour of its own.
"""
from __future__ import annotations

from app.core.security_events import allow_unconfigured_bypass, emit_security_event

__all__ = ["allow_unconfigured_bypass", "emit_security_event"]
