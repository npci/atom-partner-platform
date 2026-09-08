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
   `env_file`, so an operator who set `AUTHORITY_SSRF_ALLOWED_HOSTS` exactly as the
   error message instructed saw the identical refusal after restarting. A
   correct guard with an unreachable override is indistinguishable from a broken
   guard, so the compose passthrough is asserted here too.
"""
from pathlib import Path

import pytest
import yaml

from app.authority_client import _is_private_url

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def ssrf_settings(monkeypatch):
    """Reset both SSRF knobs to their shipped defaults for each case.

    The guard reads settings at CALL time (not import time), so patching the
    live object is enough and no reload is needed.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "authority_ssrf_allowed_hosts", "", raising=False)
    monkeypatch.setattr(settings, "authority_ssrf_allow_private_networks", False, raising=False)
    # `authority_platform_url` is a THIRD input to the verdict — the configured
    # authority host waives the private-space rule (see
    # TestConfiguredAuthorityWaivesTierTwoOnly). Pin it to a value that matches
    # nothing so a case that does not set it deliberately cannot be decided by
    # whatever the ambient env happens to carry.
    monkeypatch.setattr(settings, "authority_platform_url", "https://unset.invalid", raising=False)
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
    """`AUTHORITY_SSRF_ALLOWED_HOSTS` is the narrow, preferred approval path."""

    def test_allowlisted_ip_literal_permitted(self, ssrf_settings, monkeypatch):
        url = "https://10.84.1.5/a2a-uat"
        assert _is_private_url(url) is True  # blocked before approval

        monkeypatch.setattr(ssrf_settings, "authority_ssrf_allowed_hosts", "10.84.1.5")
        assert _is_private_url(url) is False

    def test_allowlist_is_comma_separated_and_case_insensitive(self, ssrf_settings, monkeypatch):
        monkeypatch.setattr(
            ssrf_settings, "authority_ssrf_allowed_hosts", "  NPCI-UAT.Internal , 10.84.1.5  "
        )
        assert _is_private_url("https://10.84.1.5/a2a-uat") is False

    def test_non_allowlisted_private_host_still_blocked(self, ssrf_settings, monkeypatch):
        """Approving one host must not approve its neighbours."""
        monkeypatch.setattr(ssrf_settings, "authority_ssrf_allowed_hosts", "10.84.1.5")
        assert _is_private_url("https://10.84.1.6/") is True


class TestBlanketPrivateApproval:
    def test_allow_private_networks_permits_rfc1918(self, ssrf_settings, monkeypatch):
        monkeypatch.setattr(ssrf_settings, "authority_ssrf_allow_private_networks", True)
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
        monkeypatch.setattr(ssrf_settings, "authority_ssrf_allow_private_networks", True)
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
        monkeypatch.setattr(ssrf_settings, "authority_ssrf_allowed_hosts", host)
        assert _is_private_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data",
            "http://127.0.0.1:9200/_cat/indices",
            "http://[::1]/",
        ],
    )
    def test_blocked_even_when_it_is_the_configured_authority(self, url, ssrf_settings, monkeypatch):
        """Naming a tier-1 address as the authority must not make it reachable.

        This is the third escape hatch and the one that got it wrong. The
        configured-authority exemption was originally written as an early
        `return False` ABOVE the resolution step, so it skipped the tier-1
        loop entirely and `AUTHORITY_PLATFORM_URL=http://169.254.169.254/`
        turned the cloud metadata service into an approved target — while the
        comment above it asserted the tier-1 checks still ran. It waives
        tier 2 only, which means it has to join `allow_private` and fall
        through rather than short-circuit.
        """
        monkeypatch.setattr(ssrf_settings, "authority_platform_url", url)
        assert _is_private_url(url) is True


class TestConfiguredAuthorityWaivesTierTwoOnly:
    """The exemption must still DO its job: a private authority is reachable.

    Without this, a host install pointed at an RFC-1918 authority is refused
    until the operator finds AUTHORITY_SSRF_ALLOWED_HOSTS, and the symptom
    reads as the authority being down rather than as a guard decision.
    """

    def test_private_configured_authority_is_permitted(self, ssrf_settings, monkeypatch):
        monkeypatch.setattr(ssrf_settings, "authority_platform_url", "https://10.84.12.34/a2a-uat")
        assert _is_private_url("https://10.84.12.34/a2a-uat") is False

    def test_port_and_path_do_not_affect_the_host_match(self, ssrf_settings, monkeypatch):
        monkeypatch.setattr(ssrf_settings, "authority_platform_url", "https://10.84.12.34:8443/a2a")
        assert _is_private_url("https://10.84.12.34:9999/somewhere/else") is False

    def test_a_different_private_host_is_still_blocked(self, ssrf_settings, monkeypatch):
        """The waiver is for the configured peer, not for private space at large."""
        monkeypatch.setattr(ssrf_settings, "authority_platform_url", "https://10.84.12.34/a2a-uat")
        assert _is_private_url("https://10.84.99.99/") is True

    def test_unset_authority_matches_nothing(self, ssrf_settings, monkeypatch):
        """An empty setting must not become a wildcard."""
        monkeypatch.setattr(ssrf_settings, "authority_platform_url", "")
        assert _is_private_url("https://10.84.12.34/") is True


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
            "AUTHORITY_PLATFORM_URL",
            "AUTHORITY_SSRF_ALLOWED_HOSTS",
            "AUTHORITY_SSRF_ALLOW_PRIVATE_NETWORKS",
            # Same omission, one field later, and it fails more quietly than the
            # SSRF one: a dropped PARTNER_PUBLIC_URL leaves config.py's default
            # in the AgentCard, so DISCOVERY STILL RETURNS 200 and only the
            # authority's subsequent JSON-RPC connect fails. The authority
            # operator reads that as "the partner is down"; nothing is logged
            # here, because nothing here was ever called. `.env` spends six
            # lines warning about exactly this and the value could not arrive.
            "PARTNER_PUBLIC_URL",
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

    def test_legacy_platform_url_still_reaches_the_container(self):
        """An operator whose .env still says NPCI_PLATFORM_URL must not break.

        config.py's AliasChoices accepts either spelling, but that only helps
        once a value REACHES the container — and compose interpolation has no
        aliasing of its own. So the compose entry falls back to the old variable
        explicitly. Without that nested default the rename would silently drop
        the setting for every existing deployment, and pydantic would then use
        its own default rather than erroring: exactly the class of silent
        misconfiguration this test class exists to catch.
        """
        value = str(self._backend_env()["AUTHORITY_PLATFORM_URL"])
        assert "NPCI_PLATFORM_URL" in value, (
            "AUTHORITY_PLATFORM_URL does not fall back to NPCI_PLATFORM_URL. "
            f"Got {value!r}. Existing operator .env files still use the old name."
        )

    def test_domain_pack_reaches_the_container(self):
        """Without this line the domain seam is unreachable in Docker.

        `core/domain/registry.py` reads DOMAIN_PACK from os.environ, and
        backend/.env never reaches the container. So an operator who set
        DOMAIN_PACK in their .env would get the generic pack anyway — every
        screen and the sign-off PDF rendering neutral vocabulary while they
        believed they had selected a domain. It fails silently because falling
        back to generic is a valid state, not an error.

        The default must point at a pack that EXISTS inside the image, or the
        registry raises PackError at boot for everyone.
        """
        value = str(self._backend_env()["DOMAIN_PACK"])
        assert "${DOMAIN_PACK" in value, (
            f"DOMAIN_PACK must interpolate from the host environment; got {value!r}"
        )
        default = value.split(":-", 1)[1].rstrip("}")
        assert default.startswith("/app/app/packs/"), (
            f"default pack path {default!r} is not an in-image path"
        )
        # /app/app/packs/<x>/<x>.yaml maps to backend/app/packs/<x>/<x>.yaml.
        on_disk = _REPO_ROOT / "backend" / default[len("/app/"):]
        assert on_disk.is_file(), (
            f"compose defaults DOMAIN_PACK to {default}, which does not exist "
            f"in the image (looked for {on_disk}). Every container would fail "
            f"to boot with PackError."
        )

    def test_ssrf_defaults_remain_closed(self):
        """Adding the passthrough must not loosen the shipped default."""
        env = self._backend_env()
        assert env["AUTHORITY_SSRF_ALLOWED_HOSTS"] == "${AUTHORITY_SSRF_ALLOWED_HOSTS:-}"
        assert env["AUTHORITY_SSRF_ALLOW_PRIVATE_NETWORKS"] == (
            "${AUTHORITY_SSRF_ALLOW_PRIVATE_NETWORKS:-false}"
        )

    def test_backend_declares_no_env_file(self):
        """Guards the premise above: if an env_file is ever added, revisit this."""
        compose = yaml.safe_load((_REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        assert "env_file" not in compose["services"]["backend"]


class TestAdvertisedRpcUrlIsAnOrigin:
    """`PARTNER_PUBLIC_URL` is an ORIGIN; the card appends the RPC path itself.

    Getting the passthrough right (above) is only half of it — the VALUE has a
    shape constraint that nothing enforced, and the wrong shape fails in the
    same silent way. `partner_card.py` builds the advertised interface as
    `f"{partner_public_url.rstrip('/')}/a2a-rpc/rpc"`, and the edge serves
    `/a2a-rpc/` at the ROOT. `/a2a-partner/` is the SPA. So the natural-looking
    value `https://host:8443/a2a-partner` — the UI mount an operator can see in
    their own browser, and the value the shipped templates used to suggest —
    advertises `/a2a-partner/a2a-rpc/rpc`, which nginx routes to the frontend.
    The authority's POST gets `405 Not Allowed` as `text/html` from nginx and
    never reaches this process.

    Both halves are asserted together because either alone is a green test over
    a broken deployment: the routing could move out from under the templates, or
    a template could grow a path back.
    """

    @staticmethod
    def _edge_conf() -> str:
        return (_REPO_ROOT / "deploy" / "edge.nginx.conf").read_text(encoding="utf-8")

    def test_edge_serves_rpc_at_the_root_not_under_the_ui_mount(self):
        """The premise the shape constraint rests on."""
        conf = self._edge_conf()
        assert "location /a2a-rpc/ {" in conf, (
            "The edge no longer serves /a2a-rpc/ at the root. If the RPC endpoint "
            "moved under a prefix, PARTNER_PUBLIC_URL's shape rule and the "
            "advertised card URL both have to move with it."
        )
        assert "location /a2a-partner/a2a-rpc/" not in conf

    @pytest.mark.parametrize(
        "source",
        ["docker-compose.yml", "backend/.env.example", ".env"],
    )
    def test_shipped_values_carry_no_path(self, source):
        from urllib.parse import urlparse

        path = _REPO_ROOT / source
        if not path.is_file():
            pytest.skip(f"{source} is not present in this checkout")

        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "PARTNER_PUBLIC_URL" not in stripped:
                continue
            # `KEY=value` (.env) or `KEY: value` (compose). Take the RHS and drop
            # any ${VAR:-default} wrapper so the DEFAULT is what gets checked.
            _, _, raw = stripped.partition("=" if "=" in stripped else ":")
            raw = raw.strip().strip('"').strip("'")
            if raw.startswith("${"):
                if ":-" not in raw:
                    continue  # pure passthrough, no default to check
                raw = raw.split(":-", 1)[1].rstrip("}")
            # A nested ${PORT:-8443} may remain; the port is not what we check.
            raw = raw.replace("${PARTNER_EDGE_HTTPS_PORT:-8443}", "8443")
            if not raw or "${" in raw:
                continue

            advertised_path = urlparse(raw).path
            assert advertised_path in ("", "/"), (
                f"{source} sets PARTNER_PUBLIC_URL={raw!r}, which has path "
                f"{advertised_path!r}. partner_card.py appends '/a2a-rpc/rpc', so "
                f"this advertises {raw.rstrip('/')}/a2a-rpc/rpc — a path the edge "
                f"does not route to the backend. Discovery still succeeds and "
                f"every JSON-RPC connect gets nginx's 405 text/html instead."
            )


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
            "AUTHORITY_SSRF_ALLOWED_HOSTS=10.84.12.34\n",
            encoding="utf-8",
        )
        loaded = Settings(_env_file=str(env_file))
        assert loaded.authority_ssrf_allowed_hosts == "10.84.12.34"
        assert loaded.authority_platform_url == "https://10.84.12.34/a2a-uat"


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
