"""`meiva` CLI entry — argparse dispatch to the `cmd_*` implementations in `cli.commands`.

Registered in `pyproject.toml [project.scripts] meiva = "cli.main:entry"`; after
`uv sync`, `.venv/bin/meiva` is callable. Ops layer only, decoupled from the
`meiva.*` SDK — agent should not see cron / infra plumbing.

The argparse tree is composed by `cli.parsers.build_parser`. Settings-free
parser modules own both builders and `_h_*` adapters: agents/notices live
beside their implementations in `cli.commands.agents.parsers`, while the
other domains remain under `cli.parsers`. Each builder binds its own handler
with `set_defaults(func=_h_x)`; dispatch is one `args.func(args)` call. Tests
patch the owning parser module before building the tree, so its binding reads
the patched module global. No command registry or re-export is involved.
Runtime command modules are imported only inside handler bodies. Their
Settings construction can fail on an unconfigured home; main() reports that
failure with an environment template instead of a raw traceback.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pydantic import ValidationError

from base.host.env.bootstrap import BootstrapFetchError
from base.host.env.dotenv_boot import LAUNCHER_PROFILE_ENV_KEY
from base.native_process.os_platform import (
    LockTimeoutError,
    ensure_line_buffered_stdio,
)

# Only the settings-free parser composition entry is imported here. Domain
# adapters lazy-import runtime commands when their bound handler is dispatched.
from cli import parsers
from cli.startup.log_sinks import cli_log_name
from cli.startup.settings_failure import settings_failure_text

if TYPE_CHECKING:
    from base.cluster.machine import MachineIdentityReader
    from cli.database import OpenDatabase, OperatorDatabaseFactory, OperatorEventPipeline

__all__ = [
    "ProducerFactory",
    "bind_entry_facts",
    "bind_init_inputs",
    "entry",
    "main",
    "print_settings_load_failure",
    "run_command",
]

# meiva:note
# Builds the command's telemetry producer from its database factory, machine identity and home.
type ProducerFactory = Callable[
    [OperatorDatabaseFactory, MachineIdentityReader, Path], OperatorEventPipeline
]

# Settings-lite verbs — they must construct Settings while the gateway is down
# (stop and status must remain available for recovery inspection), so
# `cli.main` opts them out of the gateway config fetch that every other process
# performs at Settings build. The fetch decision itself is role-derived
# (base.host.env.bootstrap.config_source_is_local; MEIVA_CONFIG_SOURCE is gone).
_LITE_VERBS = frozenset(
    {
        "stop",
        # `restart` is deliberately NOT here: its preflight registers this
        # machine in the central DB and its start leg needs the cluster config
        # regardless, so on a pure runner a lite restart could only ever hit
        # the never-dialed placeholder DB URL (PlaceholderDbUrlError). With the gateway down, restart now fails at
        # the fetch with the actionable BootstrapFetchError — `stop` stays lite
        # and remains the recovery verb.
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
# Print the env template an unconfigured home's Settings failure needs; the exit status.
def print_settings_load_failure(e: ValidationError) -> int:
    print(settings_failure_text(e), file=sys.stderr)
    return 1


def normalize_process_profile() -> None:
    """Pop the launcher's process profile, recording it for the boot pass.

    The CLI is a settings-full process and must never inherit a launcher-set
    process profile: with no marker, profiles.py constructs every domain as
    before. Importing base.config.profiles initializes base.config first,
    so that constant cannot be used before this cleanup without constructing
    Settings. First start resolves and persists unit identity before importing
    cli.commands; parser construction remains settings-free.

    The popped value is recorded, not discarded: an agent-launched tree keeps
    the launcher's injected runner DB / Redis projections through the authority
    pass only under a live-or-recorded agent profile (base/host/env/dotenv_boot.py
    `_enforce_cluster_env_authority`), so popping without recording made that
    exemption unreachable on every CLI path — `meiva cluster health-probe` run
    from an agent child on a pure agent-runner fell back to the sentinel
    (#4334). The record is never cleared: a nested CLI overwrites it only when
    it carries a fresh live marker of its own.
    """
    launcher_profile = os.environ.pop("MEIVA_PROCESS_PROFILE", None)
    if launcher_profile:
        os.environ[LAUNCHER_PROFILE_ENV_KEY] = launcher_profile


def _opt_into_lite_config(args_in: list[str]) -> None:
    """Verbs that must run without the config fetch build settings-lite instead."""
    # base.sessions.env_forwarding does not forward this var, so processes a
    # lite verb spawns never inherit the opt-out.
    from cli.preflight import unit_already_stopped

    if (
        args_in
        and args_in[0] in _LITE_VERBS
        and (args_in[0] != "stop" or "--force" in args_in or unit_already_stopped())
    ):
        os.environ.setdefault("MEIVA_CONFIG_FETCH", "skip")
        _deliver_lite_api_token()


# meiva:note
# Hand `meiva init` the host facts of its caller, the process facts beyond its site.
#
# Init reads none of them itself: the caller's tool directories, which ports of the
# fixed table something already holds and the transport key of a database-capability bundle
# (`args.transport_key`, taken out of the environment by `_take_transport_key`). A
# `--ssl-cert-file` is pinned into the process before dispatch because the gateway join that
# follows dials with it.
def bind_init_inputs(args: argparse.Namespace) -> None:
    from base import cluster
    from cli.startup.service_path import CallerHost

    table = cast("dict[str, int]", cluster.new_home_ports())
    args.caller_host = CallerHost(
        service_path=os.environ.get("MEIVA_SERVICE_PATH"),
        path=os.environ.get("PATH", ""),
        virtual_env=os.environ.get("VIRTUAL_ENV"),
        prefix_venv=sys.prefix if sys.prefix != sys.base_prefix else None,
        occupied_ports=frozenset(port for port in table.values() if not cluster.port_free(port)),
        transport_key=args.transport_key,
    )
    if args.ssl_cert_file is not None:
        os.environ["SSL_CERT_FILE"] = args.ssl_cert_file


# meiva:note
# Take the transport key of a database-capability bundle out of the environment, the one place
# either verb that carries a bundle (`meiva init` and `meiva cluster db-authority install-unit`)
# reads it.
#
# The key leaves the process environment before any step runs, so no child process any of those
# steps spawns inherits it; the verb receives it as `args.transport_key`. Every other verb
# leaves the variable alone.
def _take_transport_key(args_in: list[str]) -> str | None:
    if args_in[:1] != ["init"] and args_in[:3] != ["cluster", "db-authority", "install-unit"]:
        return None
    from base.host.db_authority.unit import CAPABILITY_KEY_ENV

    return os.environ.pop(CAPABILITY_KEY_ENV, "")


# meiva:note
# Hand the verb what only the process entry can read: the site `init` and `start` act on (the
# home and the checkout whose CLI runs) and the transport key of a capability bundle.
def bind_entry_facts(
    args: argparse.Namespace, args_in: list[str], *, home: Path, checkout: Path
) -> None:
    transport_key = _take_transport_key(args_in)
    if transport_key is not None:
        args.transport_key = transport_key
    if args_in[:1] in (["init"], ["start"]):
        from cli.start_runtime import HomeSite

        args.site = HomeSite(home, checkout)


def _deliver_lite_api_token() -> None:
    """Give an operator's lite CLI the runner token from its installed capability.

    Full runner boot consumes the same capability before fetching gateway config.
    Lite verbs skip that boot pass, so they need its API token alone. A launcher
    descendant must keep its delivered authority boundary, even when a token is
    missing; no source leaves the existing bearer behavior in place.
    """
    from base.host.env.bootstrap import config_source_is_local, launcher_context
    from base.host.env.dotenv_file import process_env_file, read_env_aliases
    from base.host.net.auth import API_TOKEN_ENV
    from base.paths.home import resolve_meiva_home

    if os.environ.get(API_TOKEN_ENV) or launcher_context(os.environ) is not None:
        return
    if config_source_is_local(os.environ, env_file=read_env_aliases(process_env_file())):
        return

    from base.host.db_authority.model import AuthorityRefusedError
    from base.host.db_authority.unit import consume_unit

    try:
        capability = consume_unit(resolve_meiva_home().resolve())
    except (AuthorityRefusedError, OSError, ValueError):
        return
    os.environ[API_TOKEN_ENV] = capability.api.token


def main(
    argv: list[str] | None = None,
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> int:
    """Run one command with the operator event pipeline as its telemetry producer."""
    from base.deploy import start_trace

    start_trace.begin(enabled=start_trace.requested(os.environ))
    from cli.database import open_operator_database

    return run_command(
        argv,
        retained_children=retained_children,
        producer_factory=_operator_producer,
        open_database=open_operator_database,
    )


def _operator_producer(
    database_factory: OperatorDatabaseFactory, machine: MachineIdentityReader, home: Path
) -> OperatorEventPipeline:
    from cli.database import operator_event_pipeline

    return operator_event_pipeline(database_factory, machine=machine, home=home)


# meiva:note
# Run one command; `producer_factory(database_factory, machine, home)` builds its producer and
# `open_database(config, gate, direct_url)` every database handle of the command.
def run_command(
    argv: list[str] | None,
    *,
    retained_children: list[subprocess.Popen[bytes]] | None,
    producer_factory: ProducerFactory,
    open_database: OpenDatabase,
) -> int:
    # Line-buffer stdout so a long command piped into `tee` (every detached rollout /
    # updater session) streams its own progress in real time instead of block-buffering
    # it to the end of the log, out of order against its children's unbuffered output.
    ensure_line_buffered_stdio()
    normalize_process_profile()
    args_in = sys.argv[1:] if argv is None else argv
    # The checkout gate comes first, before `boot` and before anything loads a
    # home's configuration: every command is refused unless this CLI belongs to
    # the home (`cli.preflight.require_own_checkout`).
    from cli.preflight import require_own_checkout

    checkout = Path(__file__).resolve().parents[1]
    rc = require_own_checkout(args_in, checkout)
    if rc is not None:
        return rc
    _opt_into_lite_config(args_in)

    args = parsers.parse_args(
        args_in, retained_children=[] if retained_children is None else retained_children
    )
    # meiva:note
    # The home this command acts on is read here, once, behind the gate above. `declared` is the
    # path as the environment spells it (a symlinked home is refused when it is created); `home`
    # is the resolved path every later owner (the init/start site, the command runtime, the
    # producer) is handed.
    from base.paths.home import resolve_meiva_home

    declared = resolve_meiva_home()
    home = declared.resolve()
    bind_entry_facts(args, args_in, home=home, checkout=checkout)
    if args_in[:1] == ["init"]:
        bind_init_inputs(args)
        return args.func(args)
    if args_in[:1] == ["start"]:
        from cli.start_identity import require_initialized

        try:
            require_initialized(args.site.home)
        except (ValueError, TypeError, RuntimeError, OSError) as exc:
            print(f"meiva start: {exc}", file=sys.stderr)
            return 1
    from base.cluster.machine import MachineRoleInvalid
    from base.paths.private_dir import ensure_private_dir
    from cli.database import operator_database_factory
    from cli.database.runtime import CommandRuntime

    # meiva:note
    # The one place the operator's home is created (owner-only, a symlinked home refused); the
    # runtime built next only holds the path.
    ensure_private_dir(declared)
    command = CommandRuntime.create(home)
    args.runtime = command
    database_factory = operator_database_factory(
        local_host=command.machine.host,
        open_database=open_database,
    )
    args.database_factory = database_factory
    producer = producer_factory(database_factory, command.machine, command.home)
    args.producer = producer
    try:
        if args_in[:1] not in (["init"], ["start"]):  # these two open their own, after admission
            sink = cli_log_name(args_in)
            if sink is not None:
                producer.initialize_logging(sink)
        return args.func(args)
    except LockTimeoutError as exc:
        print(
            f"meiva: another local lifecycle operation is active; retry after it finishes: {exc}",
            file=sys.stderr,
        )
        return 1
    except ValidationError as exc:
        return print_settings_load_failure(exc)
    except (BootstrapFetchError, MachineRoleInvalid) as exc:
        print(f"✗ meiva: {exc}", file=sys.stderr)
        return 1
    finally:
        # A quiet verb's first batch can otherwise reach OTLP only in atexit,
        # after concurrent.futures has stopped accepting SDK resource probes.
        # Drain while the interpreter is alive; the emitter still owns shutdown.
        # No pipeline means no work, so settings-free/no-event verbs stay lazy.
        from base import telemetry

        with telemetry.failure_isolated("CLI telemetry drain"):
            telemetry.sync(timeout=5.0)
        producer.close_command(primary=sys.exception())


def entry() -> int:
    """Prepare a fresh executable process; embedded callers use main directly."""
    from base.log import prepare_process_logging

    prepare_process_logging()
    return main()


if __name__ == "__main__":
    sys.exit(entry())
