# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Inbound A2A task handlers, split per task type.

Each module owns one task type's persistence logic; `_background.py` holds the
post-receipt workers (auto-ack, cert-status, feasibility). The executor imports
`TaskReceiveRequest` + the `handle_*` functions from here.
"""
from ._types import TaskReceiveRequest
from .blocker_resolution import handle_blocker_resolution
from .blocker_status_update import handle_blocker_status_update
from .cert_completion_signoff import handle_cert_completion_signoff
from .cert_lifecycle import (
    handle_cert_case_result,
    handle_cert_config_request,
    handle_cert_execution_start,
    handle_cert_setup_notification,
    handle_cert_verdict_notification,
)
from .cert_test_response import handle_cert_test_response
from .change_communication import handle_change_communication
from .clarification_response import handle_clarification_response
from .counter_decision import handle_counter_decision
from .negotiation_frozen import handle_negotiation_frozen
from .revision_in_progress import handle_revision_in_progress
from .round_closed import handle_round_closed
from .round_opened import handle_round_opened

__all__ = [
    "TaskReceiveRequest",
    "handle_change_communication",
    "handle_clarification_response",
    "handle_counter_decision",
    "handle_blocker_resolution",
    "handle_blocker_status_update",
    "handle_cert_test_response",
    "handle_cert_completion_signoff",
    "handle_cert_case_result",
    "handle_cert_config_request",
    "handle_cert_execution_start",
    "handle_cert_setup_notification",
    "handle_cert_verdict_notification",
    "handle_negotiation_frozen",
    "handle_revision_in_progress",
    "handle_round_opened",
    "handle_round_closed",
]
