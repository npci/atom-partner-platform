"""Switch adapters — the partner's plug-in point for their own switch.

`SWITCH_ADAPTER` selects one: `static` (default, the demo profile) or a
`package.module:ClassName` path to a `SwitchAdapter` subclass the partner ships,
the same shape as `impl:` in `config/agents.yaml`.
"""
from __future__ import annotations

import importlib
import os
from functools import lru_cache

from .base import BankConfig, CaseData, SwitchAdapter

__all__ = ["BankConfig", "CaseData", "SwitchAdapter", "get_switch_adapter"]


@lru_cache(maxsize=1)
def get_switch_adapter() -> SwitchAdapter:
    """The configured adapter, built once. Tests that change `SWITCH_ADAPTER`
    call `get_switch_adapter.cache_clear()`."""
    spec = (os.environ.get("SWITCH_ADAPTER") or "static").strip()
    if spec == "static":
        from .static import StaticSwitchAdapter
        return StaticSwitchAdapter()
    module, sep, cls_name = spec.partition(":")
    if not sep or not cls_name:
        raise ValueError(
            f"SWITCH_ADAPTER={spec!r}: expected 'static' or 'package.module:ClassName'")
    cls = getattr(importlib.import_module(module), cls_name)
    adapter = cls()
    if not isinstance(adapter, SwitchAdapter):
        raise TypeError(f"SWITCH_ADAPTER={spec!r}: {cls_name} is not a SwitchAdapter")
    return adapter
