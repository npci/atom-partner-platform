# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""AR-13: PARTNER_ALLOW_UNAUTHENTICATED_A2A must never apply outside development.

Two layers, tested separately because they fail at different moments:
  * config.py refuses to START — the loud check, at deploy time.
  * allow_unconfigured_bypass() returns False at request time — the one that
    holds if the setting is mutated after boot.
"""
import pytest

from app.core.security_events import (
    _env_label,
    _is_protected_env,
    allow_unconfigured_bypass,
)


class _S:
    def __init__(self, app_env, allow):
        self.app_env = app_env
        self.partner_allow_unauthenticated_a2a = allow


def _patch(monkeypatch, app_env, allow):
    import app.config as cfg
    monkeypatch.setattr(cfg, "settings", _S(app_env, allow))


class TestRuntimeChokepoint:
    def test_bypass_applies_in_development(self, monkeypatch):
        _patch(monkeypatch, "development", True)
        assert allow_unconfigured_bypass() is True

    @pytest.mark.parametrize("env", ["production", "staging", "PROD", " Production "])
    def test_bypass_refused_outside_development(self, monkeypatch, env):
        _patch(monkeypatch, env, True)
        assert allow_unconfigured_bypass() is False

    def test_unrecognised_env_fails_safe(self, monkeypatch):
        """A typo must not unlock the bypass — anything that is not clearly
        development is treated as protected."""
        _patch(monkeypatch, "prod", True)
        assert allow_unconfigured_bypass() is False

    def test_flag_off_is_false_everywhere(self, monkeypatch):
        _patch(monkeypatch, "development", False)
        assert allow_unconfigured_bypass() is False


class TestEnvClassification:
    @pytest.mark.parametrize("value,protected", [
        ("development", False),
        ("  DEVELOPMENT  ", False),
        ("production", True),
        ("staging", True),
        ("", True),        # unset fails safe
        (None, True),
    ])
    def test_protected_env(self, value, protected):
        assert _is_protected_env(_S(value, True)) is protected

    def test_env_label_is_normalised(self):
        assert _env_label(_S("  Production ", True)) == "production"
        assert _env_label(_S(None, True)) == ""
