# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The fail-fast guards in `config.py` must actually refuse to boot.

These guards are import-time `raise RuntimeError(...)` statements, which means
no in-process test can exercise them — `app.config` is already imported by the
time any test runs, and re-importing a module that raised leaves a half-built
entry in `sys.modules`. So each case boots a FRESH interpreter and asserts on
the exit status and the message.

That gap mattered. `_env_is_protected()` existed and was applied to
PARTNER_ALLOW_UNAUTHENTICATED_A2A, but nothing verified the refusal fired, and
the parallel guard for INTEGRATION_TESTING_ENABLED did not exist at all while
`main.py` documented it as though it did. A guard nobody boots is a guard
nobody knows is missing.
"""
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]

# Run from a directory with no `.env`, not from `backend/`.
#
# `Settings.model_config` sets `env_file=".env"`, resolved relative to the
# INTERPRETER'S CWD. Booting from `backend/` therefore reads the developer's
# own `backend/.env`, and the first draft of this file did exactly that — the
# cleartext cases "passed the guard" because a local `.env` was quietly
# supplying PARTNER_ALLOW_HTTP=true. The subprocess must see only the
# environment each case declares.
_NO_DOTENV_CWD = "/tmp"

# The floor an interpreter needs to get as far as the guards under test.
#
# The secrets are high-entropy hex rather than readable strings on purpose:
# `core/key_strength` rejects placeholder vocabulary ('secret', 'password',
# 'test', ...) and, at APP_ENV=production, `config.py` makes that rejection
# FATAL. A readable throwaway would abort the boot before the guard under test
# ever ran, and every production case would pass for the wrong reason.
#
# They are GENERATED rather than written out for the reason spelled out in
# conftest.py: a hard-coded high-entropy hex string reads to a scanner exactly
# like a leaked key, and gitleaks flagged both of these as `generic-api-key`.
# Minting them per run keeps the gate honest instead of allowlisting a value
# that only looks like a secret. `token_hex` is the format `core/key_strength`
# calibrated its thresholds against; 24 bytes clears the 32-BYTE RFC 7518 floor
# with room to spare, since the value is hex-encoded to 48 characters.
_BASE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "PYTHONPATH": str(_BACKEND),
    "SECRET_KEY": secrets.token_hex(24),
    "SESSION_JWT_SECRET": secrets.token_hex(24),
    "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5432/db",
    "PARTNER_ALLOW_HTTP": "true",
}


def _boot(**overrides) -> subprocess.CompletedProcess:
    """Import `app.config` in a fresh interpreter under the given environment."""
    env = {**_BASE_ENV, **overrides}
    return subprocess.run(
        [sys.executable, "-c", "import app.config"],
        cwd=_NO_DOTENV_CWD, env=env, capture_output=True, text=True, timeout=120,
    )


class TestIntegrationTestingTunnelRefusedOutsideDevelopment:
    """ITA-4. The flag mounts an UNAUTHENTICATED forwarding ingress that the
    A2A rate limiter does not cover, so a protected environment must refuse it.
    """

    @pytest.mark.parametrize("app_env", ["production", "staging", "PROD", " Production "])
    def test_refuses_to_boot(self, app_env):
        proc = _boot(APP_ENV=app_env, INTEGRATION_TESTING_ENABLED="true")
        assert proc.returncode != 0, (
            f"APP_ENV={app_env!r} with INTEGRATION_TESTING_ENABLED=true booted "
            f"successfully — the tunnel would be exposed.\n{proc.stderr}"
        )
        assert "INTEGRATION_TESTING_ENABLED" in proc.stderr

    def test_unrecognised_env_fails_safe(self):
        """A typo must not be read as development."""
        proc = _boot(APP_ENV="prod", INTEGRATION_TESTING_ENABLED="true")
        assert proc.returncode != 0, proc.stderr

    def test_unset_app_env_is_development_so_compose_must_set_it(self):
        """Unset APP_ENV is NOT protected — the field defaults to
        "development" (config.py), so `_env_is_protected()` is False.

        That is the documented default and this test pins it rather than
        arguing with it. The consequence is worth stating plainly, because it
        applies to this guard and to the PARTNER_ALLOW_UNAUTHENTICATED_A2A one
        equally: BOTH are inert unless APP_ENV is actually set. "Anything not
        clearly development fails safe" is only true once something puts a
        value there — an unset variable is read as development, not as
        unknown.

        `docker-compose.yml` therefore has to set APP_ENV explicitly; without
        it the shipped stack is permanently "development" and neither refusal
        can ever fire on the documented deployment path.
        """
        proc = _boot(INTEGRATION_TESTING_ENABLED="true")
        assert proc.returncode == 0, proc.stderr

    def test_permitted_in_development(self):
        proc = _boot(APP_ENV="development", INTEGRATION_TESTING_ENABLED="true")
        assert proc.returncode == 0, proc.stderr

    def test_flag_off_boots_anywhere(self):
        proc = _boot(APP_ENV="production")
        assert proc.returncode == 0, proc.stderr


class TestUnauthenticatedA2ARefusedOutsideDevelopment:
    """AR-13's loud half. The runtime chokepoint is covered by
    test_ar13_production_bypass.py; this asserts the deploy-time refusal."""

    @pytest.mark.parametrize("app_env", ["production", "staging"])
    def test_refuses_to_boot(self, app_env):
        proc = _boot(APP_ENV=app_env, PARTNER_ALLOW_UNAUTHENTICATED_A2A="true")
        assert proc.returncode != 0, proc.stderr
        assert "PARTNER_ALLOW_UNAUTHENTICATED_A2A" in proc.stderr

    def test_permitted_in_development(self):
        proc = _boot(APP_ENV="development", PARTNER_ALLOW_UNAUTHENTICATED_A2A="true")
        assert proc.returncode == 0, proc.stderr


class TestCleartextUrlGuardCoversCredentialBearingUrls:
    """Every URL this platform sends a credential to must be in the guard.

    `ainxt_base_url` ships `Authorization: Bearer <key>` plus the full prompt;
    `partner_gitlab_url` receives the `api`-scoped PAT as `PRIVATE-TOKEN`.
    Both were outside the guard, so a cleartext value for either booted with
    neither an error nor a warning.
    """

    @pytest.mark.parametrize("var,value", [
        ("AINXT_BASE_URL", "http://gateway.internal/v1"),
        ("PARTNER_GITLAB_URL", "http://gitlab.internal"),
        ("AUTHORITY_PLATFORM_URL", "http://authority.internal"),
        ("PARTNER_PUBLIC_URL", "http://partner.internal:8001"),
    ])
    def test_cleartext_refused_without_the_opt_out(self, var, value):
        env = {k: v for k, v in _BASE_ENV.items() if k != "PARTNER_ALLOW_HTTP"}
        proc = subprocess.run(
            [sys.executable, "-c", "import app.config"],
            cwd=_NO_DOTENV_CWD, env={**env, var: value},
            capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode != 0, (
            f"{var}={value!r} booted without PARTNER_ALLOW_HTTP — a credential "
            f"would be sent in the clear.\n{proc.stderr}"
        )
        assert "cleartext" in proc.stderr.lower()

    @pytest.mark.parametrize("var,value", [
        ("AINXT_BASE_URL", "https://gateway.internal/v1"),
        ("PARTNER_GITLAB_URL", "https://gitlab.example.com"),
    ])
    def test_https_is_accepted(self, var, value):
        env = {k: v for k, v in _BASE_ENV.items() if k != "PARTNER_ALLOW_HTTP"}
        proc = subprocess.run(
            [sys.executable, "-c", "import app.config"],
            cwd=_NO_DOTENV_CWD,
            env={**env, var: value,
                 # The other guarded URLs default to http://, so they have to
                 # be lifted too or they mask the case under test.
                 "AUTHORITY_PLATFORM_URL": "https://a.example.com",
                 "PARTNER_PUBLIC_URL": "https://p.example.com",
                 "OLLAMA_URL": "https://o.example.com"},
            capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
