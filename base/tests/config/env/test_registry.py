# meiva:note
# Derivation-rule tests for the env registry (R2 convergence point A).
#
# The old seam tests (test_profile_env_keys.py) held hand-written snapshots
# against the field registry. The snapshots are gone — the registry's projections
# (`base/host/env/registry.py`) are pure functions of the Settings class metadata.
# These tests pin the DERIVATION RULES instead: each projection is re-computed
# here independently from the raw metadata (`FIELD_REFS` + scope/capability/alias),
# so a future change to a rule (e.g. reverting to capability-derived sets — the
# #1570 P0 shape) is a deliberate, test-breaking change. Structural invariants
# (A1/A3) are pinned alongside.

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

import pytest

# meiva:note
# Ensure settings-lite so we can import config without a real .env
os.environ["MEIVA_CONFIG_FETCH"] = (
    # meiva:note
    # assignment, not setdefault: a setdefault would silently keep an inherited value (the login-shell .env leak class) instead of pinning settings-lite
    "skip"
)


def _fields() -> dict:
    # meiva:note
    # The raw flat field registry (name -> _FieldRef with scope/capability).
    from base.config import FIELD_REFS, field_alias, schema_extra

    return {
        name: {
            "alias": field_alias(name),
            "scope": schema_extra(ref.info).get("scope"),
            "capability": ref.capability,
        }
        for name, ref in FIELD_REFS.items()
    }


def _aliases_with(
    *, scope: tuple[str, ...] | None = None, capability: str | None = None
) -> frozenset[str]:
    # meiva:note
    # Independent recomputation of a scope/capability slice of the registry.
    out = set()
    for _name, meta in _fields().items():
        if scope is not None and meta["scope"] not in scope:
            continue
        if capability is not None and meta["capability"] != capability:
            continue
        out.add(meta["alias"])  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    return frozenset(out)  # pyright: ignore[reportUnknownArgumentType]


class TestScopeDerivationRules:
    # meiva:note
    # The scope-derived projections must equal an independent registry scan —
    # a new field with the right scope metadata lands in every correct projection
    # (A3); a rule change (e.g. deriving from capability — the #1570 P0 shape) is
    # a deliberate, test-breaking change.

    def test_cluster_scope_is_cluster_pinned_plus_cluster_default(self) -> None:
        from base.host.env.registry import cluster_scope_aliases

        expected = _aliases_with(scope=("cluster-pinned", "cluster-default"))
        assert cluster_scope_aliases() == expected
        # meiva:note
        # the six-gap class lives in this set
        assert len(expected) > 150

    def test_session_forward_is_host_scope(self) -> None:
        from base.host.env.registry import MEIVA_HOME_ENV, session_forward_keys

        # meiva:note
        # The home is no Settings field; it rides every child as a declared key.
        expected = _aliases_with(scope=("host",)) | {MEIVA_HOME_ENV}
        assert session_forward_keys() == expected
        # meiva:note
        # The F-s3-4 headline: per-agent identity never rides a daemon session.
        assert "MEIVA_AGENT_ID" not in session_forward_keys()

    def test_session_forward_carries_the_ambient_passthroughs(self) -> None:
        from base.host.env.registry import HOST_PASSTHROUGH_KEYS

        assert (
            frozenset({"DISPLAY", "WAYLAND_DISPLAY", "HOME", "USER", "LOGNAME"})
            == HOST_PASSTHROUGH_KEYS
        )

    @pytest.mark.parametrize("role", ["gateway", "runner"])
    def test_child_env_carries_the_login_identity(self, role: Literal["gateway", "runner"]) -> None:
        # meiva:note
        # A PTY child without USER reads a logged-in macOS Claude Code CLI as
        # "Not logged in" (its keychain lookup keys on the account name).
        from base.host.env.registry import child_env

        env = child_env(role, {"USER": "operator", "LOGNAME": "operator"})
        assert env["USER"] == "operator"
        assert env["LOGNAME"] == "operator"

    def test_child_env_carries_the_machine_network_proxy_configuration(self) -> None:
        # meiva:note
        # Issue #2095: the machine's proxy configuration is ambient host config —
        # the egress a building child (`npm run build` fetching Google Fonts for
        # next/font) cannot re-source itself — so every role's child env carries it,
        # in both spellings (a shell exports HTTP(S)_PROXY; npm/node/curl read the
        # lowercase set), non-empty only (an empty ALL_PROXY means "direct").
        from base.host.env.registry import NETWORK_PROXY_KEYS, child_env, network_proxy_configured

        assert {
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
        } == NETWORK_PROXY_KEYS
        direct: dict[str, str] = {}
        proxied = {
            "HTTPS_PROXY": "http://127.0.0.1:7897",
            "no_proxy": "localhost,127.0.0.1",
            "ALL_PROXY": "",
        }
        for role in ("gateway", "runner"):
            assert not (NETWORK_PROXY_KEYS & set(child_env(role, direct)))
        # meiva:note
        # The family's single reader (the Feishu ws handshake asks it): any
        # non-empty proxy key, either spelling.
        assert network_proxy_configured(proxied) is True
        assert network_proxy_configured(direct) is False
        for role in ("gateway", "runner"):
            env = child_env(role, proxied)
            assert env["HTTPS_PROXY"] == "http://127.0.0.1:7897"
            assert env["no_proxy"] == "localhost,127.0.0.1"
            assert "ALL_PROXY" not in env

    def test_the_cluster_secret_rides_no_forwarded_view(self) -> None:
        # meiva:note
        # The human bearer stays on the gateway: no daemon / session child receives
        # it, even when the launcher's environment carries it.
        from base.host.env.registry import child_env, session_forward_keys

        launcher = {"MEIVA_CLUSTER_SECRET": "human-bearer"}
        assert "MEIVA_CLUSTER_SECRET" not in session_forward_keys()
        for role in ("gateway", "runner"):
            assert "MEIVA_CLUSTER_SECRET" not in child_env(role, launcher)

    def test_agent_runner_cluster_aliases_are_capability_plus_cluster_scope(self) -> None:
        from base.host.env.registry import agent_runner_cluster_aliases

        expected = _aliases_with(
            capability="agent-runner", scope=("cluster-pinned", "cluster-default")
        )
        assert agent_runner_cluster_aliases() == expected
        # meiva:note
        # Disjoint from the host-scope session view (host-scope agent-runner
        # keys are deliberately kept on the gateway — single-box daemons).
        from base.host.env.registry import session_forward_keys

        assert not (agent_runner_cluster_aliases() & session_forward_keys())


class TestDaemonChildEnv:
    def test_child_env_refuses_the_agent_role(self) -> None:
        from base.host.env.registry import child_env

        removed_role: Any = "agent"
        with pytest.raises(ValueError, match="daemon role"):
            child_env(removed_role, {})

    def test_session_view_carries_the_gateway_url_and_port(self) -> None:
        from base.host.env.registry import session_forward_keys

        assert {"MEIVA_GATEWAY_URL", "MEIVA_GATEWAY_PORT"} <= session_forward_keys()


class TestConsumptionMatrixDeclarations:
    # meiva:note
    # The explicit consumption-matrix rows (identity / derived / seed / health
    # ports) stay exactly the declared facts — declared by FIELD NAME so an alias
    # rename follows automatically.

    def test_identity_keys_are_the_home_owned_identity_and_tool_fields(self) -> None:
        from base.host.env.registry import env_identity_keys

        expected = _aliases_with(scope=("host",)) & {
            "MEIVA_MACHINE_SERVE_GATEWAY",
            "MEIVA_MACHINE_SERVE_AGENT_RUNNER",
            "MEIVA_MACHINE_SERVE_OBSERVABILITY_STATION",
            "MEIVA_MACHINE_HOST",
            "MEIVA_MACHINE_NAME",
            "MEIVA_MACHINE_DESCRIPTION",
            "MEIVA_GATEWAY_URL",
            "MEIVA_REDIS_BIN_DIR",
        }
        # meiva:note
        # memory_remote is cluster-pinned (a remote is cluster config) but still
        # machine identity.
        expected |= {"MEIVA_MEMORY_REMOTE"}
        assert env_identity_keys() == expected
        assert "MEIVA_HOME" not in env_identity_keys()

    def test_derived_keys_are_the_derive_env_surface(self) -> None:
        from base.host.env.registry import derived_env_keys, health_port_env_aliases

        expected = {
            "MEIVA_CLUSTER_SECRET",
            "MEIVA_GATEWAY_PORT",
            "MEIVA_GATEWAY_URL",
            "MEIVA_GATEWAY_HEALTH_URL",
            "MEIVA_FRONTEND_HEALTHCHECK_URL",
            "MEIVA_APP_PORT",
            "MEIVA_MEMORY_SEARCH_PORT",
            "MEIVA_MEMORY_SEARCH_URI",
            "MEIVA_BROWSER_CDP_PORT",
            "MEIVA_PERMISSIONS_HELPER_PORT",
            "MEIVA_DB_URL",
            "MEIVA_REDIS_URL",
            "MEIVA_REDIS_ADMIN_PASSWORD",
            "MEIVA_REDIS_PASSWORD",
            "MEIVA_EVENTS_CHANNEL",
        } | set(health_port_env_aliases().values())
        assert derived_env_keys() == expected

    def test_health_port_aliases_are_host_scope_settings_fields(self) -> None:
        from base.host.env.registry import health_port_env_aliases

        aliases = health_port_env_aliases()
        assert set(aliases) == {
            "labeler",
            "heartbeat",
            "task_maintenance",
            "events_maintenance",
            "pg_backup",
            "ttl_reaper",
            "schedule_manager",
            "memory_indexer",
            "ops",
            "delivery_watchdog",
            "im_bridge",
            "page_server",
            "agent_host",
        }
        meta = _fields()
        for svc, alias in aliases.items():
            assert meta[f"{svc}_health_port"]["alias"] == alias
            assert meta[f"{svc}_health_port"]["scope"] == "host"


class TestRegistryInvariants:
    # meiva:note
    # A1/A2: every key the projections touch is registered exactly once, and
    # the projections are pure functions of the registry (no hand-written set).

    def test_passthrough_rows_never_collide_with_settings_aliases(self) -> None:
        # meiva:note
        # A1: a key declared as both a Settings alias and a passthrough row is
        # the duplicate-declaration drift class — the registry refuses it at the
        # first projection call.
        from base.host.env.registry import child_env

        # meiva:note
        # Exercises _ensure_validated(); raises RuntimeError on a collision.
        child_env("gateway", {})

    def test_every_projection_key_is_registered(self) -> None:
        # meiva:note
        # A1: no orphan keys — every alias a projection emits is either a
        # Settings field alias or a declared passthrough row.
        import base.host.env.registry as er

        registered = set(_aliases_with(scope=())) | _all_aliases()
        registered |= er.HOST_PASSTHROUGH_KEYS
        registered |= {
            er.MEIVA_HOME_ENV,
            "SSL_CERT_FILE",
            "REQUESTS_CA_BUNDLE",
            "MEIVA_AGENT_CONFIG_OVERLAY",
            "MEIVA_AGENT_BIRTH_CONFIG",
            "MEIVA_REDIS_PASSWORD",
            "PATH",
            "VIRTUAL_ENV",
            "TMPDIR",
            "TEMP",
            "TMP",
        }
        for proj in (
            er.cluster_scope_aliases(),
            er.agent_runner_cluster_aliases(),
            er.env_identity_keys(),
            er.derived_env_keys(),
            er.session_forward_keys(),
        ):
            assert proj <= registered, f"projection carries unregistered keys: {proj - registered}"


def _all_aliases() -> frozenset[str]:
    return _aliases_with(scope=("cluster-pinned", "cluster-default", "host", "agent"))


def test_env_registry_imports_on_clean_env_without_config_package() -> None:
    # meiva:note
    # Task #1099 regression: importing base.host.env.registry first (a first `meiva start`
    # prepares identity before any settings) must not circular-import through the
    # config package.
    #
    # Before the fix, `base.config.config_registry` built `FIELD_INFOS` at module
    # level, which ran `build_registry()` during the module import; its deferred
    # `base.config` package import re-entered the half-initialized registry
    # module (the package __init__ re-imports `field_alias` from it) and raised
    # ``ImportError: cannot import name 'field_alias' from partially initialized
    # module``. A fresh subprocess with no pre-imported `base.config`
    # reproduces that boot order.
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(Path(__file__).with_name("_env_import_child.py")),
            "registry_imports_first",
        ],
        # meiva:note
        # repo root
        cwd=Path(__file__).resolve().parents[4],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
