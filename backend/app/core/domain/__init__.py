# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The domain seam. Import from here, not from the submodules."""
from app.core.domain.pack import DomainPack, PackError, Role, RoleField, Signoff
from app.core.domain.registry import (
    DEFAULT_PACK,
    active_pack_path,
    clear_cache,
    get_active_pack,
    prompt_block,
)

__all__ = [
    "DEFAULT_PACK",
    "DomainPack",
    "PackError",
    "Role",
    "RoleField",
    "Signoff",
    "active_pack_path",
    "clear_cache",
    "get_active_pack",
    "prompt_block",
]
