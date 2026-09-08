# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The integration-testing tunnel — impure halves on the partner side.

The PURE halves are vendored into `app.a2a_common.integration_contract` and
`integration_allowlist`, byte-identical with the far platform's copies — a
tunnel whose two ends disagree about header rules or alias resolution corrupts
silently rather than failing.

`egress.py` is this side's half of the forward direction (ITA I-1): an inbound
`http_exchange_request` becomes a local HTTP call to a target THIS platform
resolves from its own allowlist. Off by default, dev-only.
"""
