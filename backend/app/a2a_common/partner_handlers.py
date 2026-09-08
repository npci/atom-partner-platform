# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Compatibility shim — inbound A2A handlers moved to `app.a2a_common.handlers`.

The 711-line monolith was split per task type (WS4). This module re-exports the
public surface so any lingering `from app.a2a_common.partner_handlers import ...`
keeps working. New code should import from `app.a2a_common.handlers`.
"""
from app.a2a_common.handlers import (  # noqa: F401
    TaskReceiveRequest,
    handle_blocker_resolution,
    handle_cert_test_response,
    handle_change_communication,
    handle_clarification_response,
)

__all__ = [
    "TaskReceiveRequest",
    "handle_change_communication",
    "handle_clarification_response",
    "handle_blocker_resolution",
    "handle_cert_test_response",
]
