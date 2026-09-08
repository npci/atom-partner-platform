# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Resolve the active domain pack.

Nothing in the codebase imports a pack directly — it asks here. That
indirection is the seam: `DOMAIN_PACK` names a YAML file and the code around
this module never knows which domain it got.

DOMAIN_PACK IS A PATH, NOT A KEY. The authority platform started with a
registered-key form (`DOMAIN_PACK=network`) resolving through a dict to a
Python class, and had to walk it back: a key means a domain can only be added
by editing the platform's source, which defeats the point of a pack. An
operator drops a YAML file anywhere and points at it.

WHY RESOLUTION IS ENVIRONMENT-ONLY, with no DB and no request context: several
agent modules build their system prompts as module-level constants evaluated at
import (`agents/test_data_suggester.py:SYSTEM_PROMPT`,
`agents/question_suggester.py:SYSTEM_PROMPT`,
`agents/design_alignment.py:_SYSTEM_PROMPT`,
`agents/revision_context.py:_SUMMARY_SYSTEM`). A registry needing a session
would be unusable from those call sites, and rewriting them all to defer
prompt construction is a much larger change than this seam is worth.

BOOTSTRAP, AND WHY A LOCAL DEFAULT STILL EXISTS. Vocabulary agreement across
the A2A boundary is owned by the authority: it publishes the wording, this side
adopts it, so one certification cannot end up described two ways. But three
consumers need vocabulary BEFORE any authority contact exists — the SPA renders
labels on first paint, the feasibility agent runs on the FIRST inbound change,
and the sign-off PDF needs an issuer. So the pack file here is the bootstrap
default, and authority-supplied vocabulary overrides it once received. Without
that, a cold start renders untranslated keys.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from app.core.domain.pack import DomainPack, PackError, load

# Resolved relative to this file, not the process cwd: the backend is started
# from different directories by compose, by pytest and by a host install, and a
# cwd-relative default silently resolves to a different file in each.
_PACKS_DIR = Path(__file__).resolve().parents[2] / "packs"
DEFAULT_PACK = str(_PACKS_DIR / "generic" / "generic.yaml")


def active_pack_path() -> str:
    """The pack file this process will use.

    No lowercasing or normalisation: this is a filesystem path, and both paths
    and this repo's own checkout can be case-sensitive.
    """
    return (os.environ.get("DOMAIN_PACK") or DEFAULT_PACK).strip()


@lru_cache(maxsize=None)
def _load_cached(path: str) -> DomainPack:
    return load(path)


def get_active_pack() -> DomainPack:
    """The active pack, parsed once per path per process.

    Raises PackError if DOMAIN_PACK points at something unloadable. That is on
    purpose and must not be softened into a fallback: a deployment that meant
    to run the library-lending vocabulary but quietly got the generic one
    produces documents that read plausibly and are wrong, which is the worst
    way to discover a misconfiguration.
    """
    return _load_cached(active_pack_path())


def prompt_block(name: str, default: str = "") -> str:
    """Module-level convenience for the common case.

    Agent modules want one block, at import time, and should not each have to
    reach for the pack object. Mirrors the authority platform's helper of the
    same name so the two repos read alike.
    """
    return get_active_pack().prompt_block(name, default)


def clear_cache() -> None:
    """Test/dev hook — drop the parsed pack so a changed DOMAIN_PACK is seen.

    The cache is keyed on the path, so tests that point DOMAIN_PACK at a
    tmp_path fixture get a fresh parse for free; this exists for the case where
    the FILE changed but its path did not.
    """
    _load_cached.cache_clear()


__all__ = [
    "DEFAULT_PACK",
    "DomainPack",
    "PackError",
    "active_pack_path",
    "clear_cache",
    "get_active_pack",
    "prompt_block",
]
