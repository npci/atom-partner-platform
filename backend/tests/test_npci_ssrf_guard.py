# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the SSRF guard on the NPCI connectivity probe (SAST finding F-003)
and for the deployment wiring that makes its escape hatch usable.

Two distinct concerns, both regressions we actually hit:

1. The guard's own logic — private space blocked by default, approvable per host
   or wholesale, with loopback/link-local never overridable.

2. The plumbing. The guard's logic was already correct, but the settings it
   reads could not reach a compose deployment: `backend/.env` is excluded from
   the image by `.dockerignore` and the `backend` service declares no
   `env_file`, so an operator who set `NPCI_SSRF_ALLOWED_HOSTS` exactly as the
   error message instructed saw the identical refusal after restarting. A
   correct guard with an unreachable override is indistinguishable from a broken
   guard, so the compose passthrough is asserted here too.
"""
from pathlib import Path

import pytest
import yaml

from app.npci_client import _is_private_url

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def ssrf_settings(monkeypatch):
    """Reset both SSRF knobs to their shipped defaults for each case.

    The guard reads settings at CALL time (not import time), so patching the
    live object is enough and no reload is needed.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "npci_ssrf_allowed_hosts", "", raising=False)
    monkeypatch.setattr(settings, "npci_ssrf_allow_private_networks", False, raising=False)
    return settings


class TestPrivateSpaceBlockedByDefault:
    """An unconfigured deployment must not be able to probe the internal network."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://10.84.1.5/a2a-uat",      # the reported NPCI UAT shape
            "https://172.16.0.1/",
            "https://192.168.1.10/",
        ],
    )
    def test_private_ip_refused(self, url, ssrf_settings):
        assert _is_private_url(url) is True

    def test_public_host_allowed(self, ssrf_settings):
        assert _is_private_url("https://example.com") is False


class TestAllowlistApprovesHost:
    """`NPCI_SSRF_ALLOWED_HOSTS` is the narrow, preferred approval path."""

    def test_allowlisted_ip_literal_permitted(self, ssrf_settings, monkeypatch):
        url = "https://10.84.1.5/a2a-uat"
        assert _is_private_url(url) is True  # blocked before approval

        monkeypatch.setattr(ssrf_settings, "npci_ssrf_allowed_hosts", "10.84.1.5")
        assert _is_private_url(url) is False

    def test_allowlist_is_comma_separated_and_case_insensitive(self, ssrf_settings, monkeypatch):
        monkeypatch.setattr(
            ssrf_settings, "npci_ssrf_allowed_hosts", "  NPCI-UAT.Internal , 10.84.1.5  "
        )
        assert _is_private_url("https://10.84.1.5/a2a-uat") is False

    def test_non_allowlisted_private_host_still_blocked(self, ssrf_settings, monkeypatch):
        """Approving one host must not approve its neighbours."""
        monkeypatch.setattr(ssrf_settings, "npci_ssrf_allowed_hosts", "10.84.1.5")
        assert _is_private_url("https://10.84.1.6/") is True


class TestBlanketPrivateApproval:
    def test_allow_private_networks_permits_rfc1918(self, ssrf_settings, monkeypatch):
        monkeypatch.setattr(ssrf_settings, "npci_ssrf_allow_private_networks", True)
        assert _is_private_url("https://10.84.1.5/a2a-uat") is False
        assert _is_private_url("https://192.168.1.10/") is False


class TestTierOneNeverOverridable:
    """Loopback and link-local are refused no matter what is configured.

    169.254.169.254 is the cloud metadata endpoint — the canonical SSRF prize,
    and never a legitimate NPCI platform. Neither escape hatch may re-enable it.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "https://169.254.169.254/latest/meta-data",
            "https://127.0.0.1/",
        ],
    )
    def test_blocked_even_with_blanket_private_approval(self, url, ssrf_settings, monkeypatch):
        monkeypatch.setattr(ssrf_settings, "npci_ssrf_allow_private_networks", True)
        assert _is_private_url(url) is True

    @pytest.mark.parametrize(
        "host,url",
        [
            ("169.254.169.254", "https://169.254.169.254/latest/meta-data"),
            ("127.0.0.1", "https://127.0.0.1/"),
        ],
    )
    def test_blocked_even_when_explicitly_allowlisted(self, host, url, ssrf_settings, monkeypatch):
        """The allowlist waives the private-space rule, not the tier-1 one."""
        monkeypatch.setattr(ssrf_settings, "npci_ssrf_allowed_hosts", host)
        assert _is_private_url(url) is True


class TestUnresolvableHostFailsClosed:
    def test_dns_failure_treated_as_unsafe(self, ssrf_settings):
        assert _is_private_url("https://no-such-host.invalid/") is True


class TestComposePassesSsrfSettingsThrough:
    """The escape hatch must be reachable in a Docker deployment.

    `backend/.env` is excluded from the image on purpose (see the
    `.dockerignore` header) and the backend service sets no `env_file`, so any
    setting the operator is expected to change MUST appear in the service's
    `environment:` block. Without this, the remedy named in the error message
    silently does nothing.
    """

    @staticmethod
    def _backend_env() -> dict:
        compose = yaml.safe_load((_REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        return compose["services"]["backend"]["environment"]

    @pytest.mark.parametrize(
        "var",
        [
            "NPCI_PLATFORM_URL",
            "NPCI_SSRF_ALLOWED_HOSTS",
            "NPCI_SSRF_ALLOW_PRIVATE_NETWORKS",
        ],
    )
    def test_variable_is_passed_through(self, var):
        env = self._backend_env()
        assert var in env, (
            f"{var} is missing from the backend service's environment block. "
            "backend/.env does not reach the container (.dockerignore excludes it "
            "and no env_file is declared), so an operator cannot apply the fix "
            "the SSRF error message recommends."
        )
        assert f"${{{var}" in str(env[var]), (
            f"{var} must interpolate from the host environment (${{{var}:-...}}), "
            "otherwise a hardcoded value overrides what the operator sets."
        )

    def test_ssrf_defaults_remain_closed(self):
        """Adding the passthrough must not loosen the shipped default."""
        env = self._backend_env()
        assert env["NPCI_SSRF_ALLOWED_HOSTS"] == "${NPCI_SSRF_ALLOWED_HOSTS:-}"
        assert env["NPCI_SSRF_ALLOW_PRIVATE_NETWORKS"] == (
            "${NPCI_SSRF_ALLOW_PRIVATE_NETWORKS:-false}"
        )

    def test_backend_declares_no_env_file(self):
        """Guards the premise above: if an env_file is ever added, revisit this."""
        compose = yaml.safe_load((_REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        assert "env_file" not in compose["services"]["backend"]


class TestNativeDeploymentCanConfigureViaEnvFile:
    """A native (non-Docker) install configures the service through `.env`.

    pydantic-settings forbids extra inputs, so any knob the operator is told to
    set MUST be a declared field. `PARTNER_ALLOW_HTTP` was read only via
    `os.getenv`, which meant putting it in `.env` — the documented native
    workflow (DEPLOYMENT_GUIDE §4.3) — aborted startup with
    "partner_allow_http: Extra inputs are not permitted" rather than suppressing
    the cleartext guard. It worked solely as a shell variable, which nothing
    documented.
    """

    def test_partner_allow_http_is_a_declared_field(self):
        from app.config import Settings

        assert "partner_allow_http" in Settings.model_fields, (
            "PARTNER_ALLOW_HTTP must be a declared setting, otherwise placing it "
            "in a .env file crashes startup with 'Extra inputs are not permitted'."
        )

    def test_declared_field_defaults_to_false(self):
        """The guard must stay fail-closed unless explicitly opted out of."""
        from app.config import Settings

        assert Settings.model_fields["partner_allow_http"].default is False

    def test_env_file_value_is_accepted(self, tmp_path, monkeypatch):
        """A `.env` carrying the flag must parse instead of raising."""
        from app.config import Settings

        env_file = tmp_path / ".env"
        env_file.write_text(
            "DATABASE_URL=postgresql+psycopg://u:p@localhost:5432/db\n"
            "PARTNER_ALLOW_HTTP=true\n",
            encoding="utf-8",
        )
        # Shell env must not mask what the file provides.
        monkeypatch.delenv("PARTNER_ALLOW_HTTP", raising=False)

        loaded = Settings(_env_file=str(env_file))
        assert loaded.partner_allow_http is True

    def test_ssrf_allowlist_is_settable_from_env_file(self, tmp_path):
        """The SSRF remedy must be applicable natively, not just via compose."""
        from app.config import Settings

        env_file = tmp_path / ".env"
        env_file.write_text(
            "DATABASE_URL=postgresql+psycopg://u:p@localhost:5432/db\n"
            "NPCI_PLATFORM_URL=https://10.84.12.34/a2a-uat\n"
            "NPCI_SSRF_ALLOWED_HOSTS=10.84.12.34\n",
            encoding="utf-8",
        )
        loaded = Settings(_env_file=str(env_file))
        assert loaded.npci_ssrf_allowed_hosts == "10.84.12.34"
        assert loaded.npci_platform_url == "https://10.84.12.34/a2a-uat"


class TestEnvExampleIsUsableNatively:
    """`cp .env.example .env` is step 3 of the native setup (§4.3).

    The template shipped only docker-compose service names for the URLs the
    cleartext guard inspects, and omitted two of them entirely — so a native
    operator following the guide hit a startup failure naming
    `partner_public_url` and `ollama_url`, neither of which appeared in the file
    they had just copied.
    """

    @staticmethod
    def _template_text() -> str:
        return (_REPO_ROOT / "backend" / ".env.example").read_text(encoding="utf-8")

    @pytest.mark.parametrize("var", ["PARTNER_PUBLIC_URL", "OLLAMA_URL", "PARTNER_ALLOW_HTTP"])
    def test_guard_relevant_vars_are_present(self, var):
        assert var in self._template_text(), (
            f"{var} is absent from .env.example, so a native operator cannot see "
            "or set it before the cleartext startup guard rejects its default."
        )

    def test_no_docker_service_hostnames_are_left_active(self):
        """Active (uncommented) lines must not point at compose-only hostnames."""
        active = [
            ln.strip()
            for ln in self._template_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        offenders = [
            ln
            for ln in active
            if any(h in ln for h in ("partner_backend", "host.docker.internal", "@partner_postgres"))
            or "//ollama:" in ln
        ]
        assert not offenders, (
            f"These active template lines use docker-only hostnames and will not "
            f"resolve in a native deployment: {offenders}"
        )
