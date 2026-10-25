# meiva:note
# Pure entry-point plans for the `meiva` CLI: lite-config opt-in, launcher profile
# recording, and capability transport-key extraction.

from __future__ import annotations

from collections.abc import MutableMapping, Sequence

__all__ = [
    "LAUNCHER_PROFILE_ENV_KEY",
    "LITE_VERBS",
    "PROCESS_PROFILE_ENV_KEY",
    "needs_stop_probe",
    "needs_transport_key",
    "normalize_profile_env",
    "select_lite_api_token",
    "uses_lite_config",
]

PROCESS_PROFILE_ENV_KEY = "MEIVA_PROCESS_PROFILE"
LAUNCHER_PROFILE_ENV_KEY = "MEIVA_LAUNCHER_PROFILE"

# meiva:note
# Settings-lite verbs — they must construct Settings while the gateway is down
# (stop and status must remain available for recovery inspection), so
# `cli.main` opts them out of the gateway config fetch that every other process
# performs at Settings build. The fetch decision itself is role-derived
# (base.host.env.bootstrap.config_source_is_local; MEIVA_CONFIG_SOURCE is gone).
#
# `restart` is deliberately NOT here: its preflight registers this machine in the
# central DB and its start leg needs the cluster config regardless, so on a pure
# runner a lite restart could only ever hit the never-dialed placeholder DB URL
# (PlaceholderDbUrlError). With the gateway down, restart now fails at the fetch
# with the actionable BootstrapFetchError — `stop` stays lite and remains the
# recovery verb.
LITE_VERBS = frozenset(
    {
        "stop",
        "status",
        "pty",
        "cluster",
        "agents",
        "config",
        "schedules",
        "logs",
        "mcp",
        "memory",
        "plugins",
        "skill",
        "permissions",
    }
)


# meiva:note
# Pop the launcher's process profile from `environ`, recording it under
# `MEIVA_LAUNCHER_PROFILE` for the boot pass.
def normalize_profile_env(environ: MutableMapping[str, str]) -> None:
    launcher_profile = environ.pop(PROCESS_PROFILE_ENV_KEY, None)
    if launcher_profile:
        environ[LAUNCHER_PROFILE_ENV_KEY] = launcher_profile


# meiva:note
# Whether `args_in` is a graceful `stop` whose lite-config decision depends on
# whether the unit is already stopped.
def needs_stop_probe(args_in: Sequence[str]) -> bool:
    return bool(args_in) and args_in[0] == "stop" and "--force" not in args_in


# meiva:note
# Whether a CLI invocation with `args_in` opts out of the gateway config fetch.
def uses_lite_config(args_in: Sequence[str], *, unit_stopped: bool) -> bool:
    if not args_in or args_in[0] not in LITE_VERBS:
        return False
    return args_in[0] != "stop" or "--force" in args_in or unit_stopped


# meiva:note
# Choose the API token a lite CLI verb should place into its environment, or None
# when the caller already holds a token, runs under a launcher context, reads
# local config, or has no installed runner capability.
def select_lite_api_token(
    *,
    explicit_bearer: str | None,
    has_launcher_context: bool,
    local_config_source: bool,
    capability_bearer: str | None,
) -> str | None:
    if explicit_bearer or has_launcher_context or local_config_source:
        return None
    return capability_bearer


# meiva:note
# Whether `args_in` is one of the two verbs (`meiva init` and
# `meiva cluster db-authority install-unit`) that consume a capability bundle's
# transport key from the environment.
def needs_transport_key(args_in: Sequence[str]) -> bool:
    return tuple(args_in[:1]) == ("init",) or tuple(args_in[:3]) == (
        "cluster",
        "db-authority",
        "install-unit",
    )
