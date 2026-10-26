"""`meiva` CLI top-level dispatch routing.

main() builds the argparse parser, parses argv, then calls `args.func(args)`
where `func` was bound at parser-build time via `set_defaults(func=...)`,
referring to the `_h_*` handler defined in its owning parser module. Each handler
lazy-imports the cmd_X impl; tests replace selected handlers on the real parser
bindings to record routing without invoking real cmd_start / cmd_cluster_status.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import socket
import subprocess
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from base import cluster
from base.host.db_authority.unit import CAPABILITY_KEY_ENV
from base.host.env.dotenv_boot import LAUNCHER_PROFILE_ENV_KEY
from base.paths.tests.homes import point_home
from cli import main as _main
from cli import parsers as _parsers
from cli.commands.agents import parsers as _agents
from cli.commands.cluster.tests.gateway_server import Reply, serving
from cli.commands.extensions.parsers import mcp as _mcp
from cli.commands.extensions.parsers import plugins as _plugins
from cli.database import OperatorEventPipeline, open_operator_database, operator_event_pipeline
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from cli.parsers import build_parser
from cli.parsers import cluster as _cluster
from cli.parsers import host as _host
from cli.parsers import logs as _logs
from cli.parsers import pty as _pty
from cli.preflight import require_own_checkout
from cli.start_runtime import HomeSite
from cli.startup.service_path import CallerHost
from cli.tests.bootstrap.init_identity import initialized_runner_home


def _subparser_children(action: argparse.Action) -> dict[str, argparse.ArgumentParser] | None:
    """The name -> parser map of a subparsers action, or None for an ordinary
    `--flag choices=(...)` action (whose `.choices` is a plain tuple/list of
    values, not a dict). Checked structurally (every choice value is itself a
    parser) rather than by `isinstance(action, argparse._SubParsersAction)` —
    that private generic narrows to an unparameterized `Unknown`, which makes
    `tests/cli`'s pyright tier (`reportUnknownMemberType = "error"`) fail on the
    resulting `.choices` access."""
    choices = action.choices
    if not isinstance(choices, dict) or not choices:
        return None
    typed = cast("dict[str, object]", choices)
    if not all(isinstance(v, argparse.ArgumentParser) for v in typed.values()):
        return None
    return cast("dict[str, argparse.ArgumentParser]", typed)


def _iter_leaf_parsers(
    parser: argparse.ArgumentParser,
) -> list[argparse.ArgumentParser]:
    """Every parser in the tree with no subcommands of its own — the ones a
    dispatch actually lands on (every domain builder's `add_subparsers` call is
    `required=True`, so a group parser itself never carries its own `func`)."""
    subparsers_actions = [
        children for action in parser._actions if (children := _subparser_children(action))
    ]
    if not subparsers_actions:
        return [parser]
    leaves: list[argparse.ArgumentParser] = []
    seen: set[int] = set()
    for children in subparsers_actions:
        for child in children.values():
            if id(child) in seen:
                continue
            seen.add(id(child))
            leaves.extend(_iter_leaf_parsers(child))
    return leaves


def test_every_leaf_subcommand_binds_a_handler_from_its_parser_module() -> None:
    """Every leaf subcommand's `func` must be a callable defined in the
    owning parser module that built it — guards against a builder left
    pointing at a handler name that was renamed or removed (e.g. a stale
    `cli.main` re-export)."""
    leaves = _iter_leaf_parsers(build_parser())
    assert len(leaves) > 100, "sanity: the walk should reach the whole command surface"
    for leaf in leaves:
        func = leaf.get_default("func")
        assert callable(func), f"{leaf.prog!r} has no callable 'func' bound"
        module = getattr(func, "__module__", "")
        if leaf.prog.startswith(("meiva agents ", "meiva notices ")):
            assert module == "cli.commands.agents.parsers", (leaf.prog, module)
        elif leaf.prog.startswith("meiva impersonate "):
            assert module == "cli.commands.agents.impersonation_parsers", (leaf.prog, module)
        elif leaf.prog.startswith(
            ("meiva mcp ", "meiva memory ", "meiva packages ", "meiva plugins ", "meiva skill ")
        ):
            assert module.startswith("cli.commands.extensions.parsers."), (leaf.prog, module)
        else:
            assert module.startswith("cli.parsers."), (leaf.prog, module)


_CHILD = Path(__file__).with_name("bootstrap") / "child_main_dispatch.py"


def _run_isolated_program(
    scenario: str, tmp_path: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed interpreter and repository-owned probe.
        [sys.executable, "-B", str(_CHILD), scenario, *args],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def test_import_defers_cli_logging_until_dispatch(tmp_path: Path) -> None:
    """A settings-free entry can import the parser before choosing its path."""
    result = _run_isolated_program("defer_logging", tmp_path)
    assert result.returncode == 0, result.stderr


# Each top-level (and nested) meiva sub-command maps to a _h_* handler defined
# in its owning settings-free parser module.
_HANDLERS: tuple[tuple[list[str], types.ModuleType, str], ...] = (
    (["stop"], _host, "h_stop"),
    (["restart"], _host, "h_restart"),
    (["status"], _host, "_h_status"),
    (["pty", "freeze", "--holder", "operator", "--reason", "cleanup"], _pty, "_h_pty_freeze"),
    (["pty", "status"], _pty, "_h_pty_status"),
    (["pty", "resume", "generation"], _pty, "_h_pty_resume"),
    (["converge", "--all-services"], _host, "_h_converge"),
    (["firewall", "status"], _host, "_h_firewall_status"),
    (["firewall", "sync"], _host, "_h_firewall_sync"),
    (["cluster", "status"], _cluster, "_h_cluster_status"),
    (["plugins", "update"], _plugins, "_h_plugins_update"),
    (["plugins", "verify"], _plugins, "_h_plugins_verify"),
    (["agents", "ls"], _agents, "_h_agents_ls"),
    (["agents", "cancel", "1"], _agents, "_h_agents_cancel"),
    (["agents", "restart", "1"], _agents, "_h_agents_restart"),
    (["agents", "terminate", "1"], _agents, "_h_agents_terminate"),
    (["agents", "kill", "1"], _agents, "h_agents_kill"),
    (["agents", "resurrect", "1"], _agents, "_h_agents_resurrect"),
    (["agents", "send", "1", "hi", "--source", "user"], _agents, "h_agents_send"),
    (["memory", "search", "context"], _mcp, "h_memory_search"),
    (["logs", "retention"], _logs, "_h_logs_retention"),
    (["logs", "rotate"], _logs, "_h_logs_rotate"),
)


@pytest.mark.parametrize(("argv", "module", "handler_name"), _HANDLERS)
def test_dispatch_invokes_per_subcommand_handler(
    argv: list[str], module: types.ModuleType, handler_name: str, tmp_path: Path
) -> None:
    """Each sub-command routes to its `_h_*` handler with the parsed Namespace."""
    args = _main.parse_command_args(argv)
    raw_func: Callable[..., int] | functools.partial[int] = args.func
    bound = raw_func.func if isinstance(raw_func, functools.partial) else raw_func
    assert bound.__module__ == module.__name__
    assert bound.__name__ == handler_name

    captured: dict[str, argparse.Namespace] = {}

    def _fake(parsed: argparse.Namespace) -> int:
        captured["args"] = parsed
        return 42

    args.func = _fake
    rc = _main.dispatch_parsed(
        args,
        argv,
        declared=tmp_path,
        checkout=Path(_main.__file__).resolve().parents[1],
        producer_factory=lambda db, machine, home: OperatorEventPipeline.recording(
            db, machine=machine, home=home, log_sinks=[]
        ),
        open_database=open_operator_database,
    )
    assert rc == 42
    assert captured["args"] is args


def test_cli_discards_an_inherited_process_profile() -> None:
    """The CLI always constructs the full settings domain set."""
    env = {"MEIVA_PROCESS_PROFILE": "agent"}
    _main.normalize_profile_env(env)
    assert "MEIVA_PROCESS_PROFILE" not in env
    assert env[LAUNCHER_PROFILE_ENV_KEY] == "agent"


def test_restart_handler_forwards_the_parsed_config_overlay(
    command_runtime: CommandRuntime,
) -> None:
    """The restart parser keeps the JSON string intact for its HTTP command."""
    args = _main.parse_command_args(
        ["agents", "restart", "8", "--config", '{"llm_model":"gpt-5.6-sol"}']
    )
    assert args.agent_id == 8
    assert args.config == '{"llm_model":"gpt-5.6-sol"}'
    assert args.source is None
    with serving({("POST", "/api/agents/8/restart"): Reply(body={"status": "restarted"})}) as gw:
        command_runtime.config.set_field("gateway_url", gw.url)
        command_runtime.config.set_field("cluster_secret", "test-secret")
        args.runtime = command_runtime
        assert args.func(args) == 0
    assert [(req.method, req.target, req.body) for req in gw.requests] == [
        ("POST", "/api/agents/8/restart", {"config_overlay": {"llm_model": "gpt-5.6-sol"}})
    ]


def test_migrations_subcommand_removed() -> None:
    """`meiva migrations apply` is gone — migration is now a side-effect of
    `meiva start`. argparse exits 2 on the unknown subcommand."""
    with pytest.raises(SystemExit):
        _parsers.parse_args(["migrations", "apply"])


@pytest.mark.parametrize(
    "argv",
    [
        ["cluster", "update", "--prepared", "/private/request.json"],
        ["cluster", "release", "status"],
        ["cluster", "pitr", "activate"],
    ],
)
def test_image_release_and_pitr_activation_verbs_are_removed(argv: list[str]) -> None:
    """The retained-image release and PITR-activation operator verbs no longer parse."""
    with pytest.raises(SystemExit) as exited:
        _parsers.parse_args(argv)

    assert exited.value.code == 2


def test_logs_retention_parser_accepts_the_public_flags() -> None:
    """The local-log cleanup contract is reachable at `meiva logs retention`."""
    args = _parsers.parse_args(
        ["logs", "retention", "--family-days", "gateway=31,ops=30", "--dry-run"]
    )

    assert args.family_days == {"gateway": 31, "ops": 30}
    assert args.dry_run is True


def test_logs_retention_parser_accepts_default_as_the_other_family() -> None:
    args = _parsers.parse_args(["logs", "retention", "--family-days", "agent=15,default=14"])

    assert args.family_days == {"agent": 15, "other": 14}


def test_logs_retention_parser_rejects_combined_age_modes() -> None:
    with pytest.raises(SystemExit):
        _parsers.parse_args(
            [
                "logs",
                "retention",
                "--older-than",
                "21",
                "--family-days",
                "agent=15",
            ]
        )


def test_logs_retention_parser_rejects_unknown_family() -> None:
    with pytest.raises(SystemExit):
        _parsers.parse_args(["logs", "retention", "--family-days", "restarter=4"])


def test_logs_retention_default_comes_from_observability_settings() -> None:
    from base.config.domains.observability.settings import ObservabilitySettings

    field = ObservabilitySettings.model_fields["log_retention_days"]
    configured = ObservabilitySettings(MEIVA_LOG_RETENTION_DAYS=23)

    assert field.alias == "MEIVA_LOG_RETENTION_DAYS"
    assert configured.log_retention_days == 23


def test_logs_retention_parser_rejects_non_positive_days() -> None:
    with pytest.raises(SystemExit):
        _parsers.parse_args(["logs", "retention", "--older-than", "0"])


def test_logs_retention_settings_reject_non_positive_environment_default() -> None:
    from pydantic import ValidationError

    from base.config.domains.observability.settings import ObservabilitySettings

    with pytest.raises(ValidationError):
        ObservabilitySettings(MEIVA_LOG_RETENTION_DAYS=0)


def test_logs_retention_help_explains_defaults_and_dry_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exited:
        _parsers.parse_args(["logs", "retention", "--help"])

    assert exited.value.code == 0
    help_text = capsys.readouterr().out
    assert "14 days" in help_text
    assert "MEIVA_LOG_RETENTION_DAYS" in help_text
    assert "agent=15" in help_text
    assert "--older-than DAYS | --family-days" in help_text
    assert "without\n                        deleting" in help_text


@pytest.mark.parametrize("verb", ["status", "retire"])
def test_removed_backup_operation_custody_verbs_are_rejected(verb: str) -> None:
    with pytest.raises(SystemExit):
        _parsers.parse_args(["backup", "operations", verb])


@pytest.mark.parametrize(
    "argv",
    [
        ["pitr", "operations", "status"],
        ["pitr", "retention", "inspect"],
        ["pitr", "drill"],
        ["pitr", "multipart", "list"],
        ["pitr", "snapshot", "verify"],
    ],
)
def test_the_removed_wal_verbs_no_longer_parse(argv: list[str]) -> None:
    """The self-written PITR stack remains removed."""
    with pytest.raises(SystemExit) as exited:
        _parsers.parse_args(argv)

    assert exited.value.code == 2


# meiva:note
# `meiva start --only-service gateway ...` reaches _h_start with the parsed
# argparse Namespace: the service selection is all `start` takes. `start` is the one
# verb with a pre-dispatch side effect (the settings-free installed-home gate); the
# home here is initialized, so the gate admits it and hands the start its site.
def test_start_subcommand_forwards_argparse_flags(unit_home: Path) -> None:
    captured: dict[str, argparse.Namespace] = {}

    def _fake(args: argparse.Namespace) -> int:
        captured["args"] = args
        return 7

    argv = ["start", "--only-service", "gateway", "--only-service", "frontend"]
    args = _main.parse_command_args(argv)
    args.func = _fake
    home = initialized_runner_home(unit_home, unit_home / "checkout")
    checkout = Path(_main.__file__).resolve().parents[1]
    rc = _main.dispatch_parsed(
        args,
        argv,
        declared=home,
        checkout=checkout,
        producer_factory=operator_event_pipeline,
        open_database=open_operator_database,
    )
    assert rc == 7
    ns = captured["args"]
    assert ns.only_service == ["gateway", "frontend"]
    assert ns.disable_service is None and ns.all_services is False
    assert ns.site.home == home.resolve()
    assert ns.site.checkout == checkout


# meiva:note
# `main` is init's composition root: it reads the home, the checkout and the caller's
# host facts once and hands them over, so `run_init` reads none of them itself.
def test_init_receives_its_site_and_the_hosts_facts(unit_home: Path) -> None:
    captured: dict[str, argparse.Namespace] = {}

    def _fake(args: argparse.Namespace) -> int:
        captured["args"] = args
        return 7

    argv = ["init", "--machine-name", "probe", "--serve-gateway"]
    args = _main.parse_command_args(argv)
    args.func = _fake
    checkout = Path(_main.__file__).resolve().parents[1]
    assert (
        _main.dispatch_parsed(
            args,
            argv,
            declared=unit_home,
            checkout=checkout,
            producer_factory=operator_event_pipeline,
            open_database=open_operator_database,
        )
        == 7
    )
    ns = captured["args"]
    assert ns.site == HomeSite(unit_home.resolve(), checkout)
    assert isinstance(ns.caller_host, CallerHost)


# meiva:note
# The transport key of a capability bundle leaves the environment at the entry, once, for the two
# verbs that carry a bundle; every other verb leaves the variable where it found it. A fresh
# process runs the entry with the key in its environment and reports what the verb received.
@pytest.mark.parametrize(
    ("argv", "received", "left_in_environment"),
    [
        (["init"], "sealed-key", None),
        (["cluster", "db-authority", "install-unit", "bundle.sealed"], "sealed-key", None),
        (["doctor"], None, "sealed-key"),
    ],
)
def test_the_transport_key_leaves_the_environment_at_the_entry_for_the_verbs_that_carry_one(
    argv: list[str], received: str | None, left_in_environment: str | None, tmp_path: Path
) -> None:
    # meiva:note
    # The child is this repository's own fixed probe script, run with the current interpreter.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-B", str(_CHILD), "transport_key", *argv],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "HOME": str(tmp_path), CAPABILITY_KEY_ENV: "sealed-key"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    seen = json.loads(result.stdout.splitlines()[-1])
    assert seen["arg"] == received
    assert seen["environment"] == left_in_environment
    if argv[0] == "init":
        assert seen["caller"] == "sealed-key"


# meiva:note
# The entry reads the home once and hands every owner the same resolved path: the command runtime
# and the producer name the home behind a symlink, and the directory is created owner-only.
def test_the_entry_resolves_a_symlinked_home_once_for_the_runtime_and_the_producer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real = tmp_path.resolve() / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    declared = point_home(monkeypatch, link / "home")
    homes: list[Path] = []

    def _fake(args: argparse.Namespace) -> int:
        homes.append(args.runtime.home)
        return 0

    args = _main.parse_command_args(["status"])
    args.func = _fake
    assert (
        _main.dispatch_parsed(
            args,
            ["status"],
            declared=declared,
            checkout=Path(_main.__file__).resolve().parents[1],
            producer_factory=operator_event_pipeline,
            open_database=open_operator_database,
        )
        == 0
    )

    assert homes == [real / "home"]
    assert (real / "home").stat().st_mode & 0o777 == 0o700


def test_init_learns_which_fixed_ports_something_already_holds(unit_home: Path) -> None:
    held = cluster.new_home_ports()["gateway"]
    args = build_parser().parse_args(["init", "--machine-name", "probe", "--serve-gateway"])
    args.transport_key = ""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", held))
        listener.listen()
        _main.bind_init_inputs(args)

    host = args.caller_host
    assert host.occupied_ports == frozenset({held})
    assert host.path == os.environ.get("PATH", "")
    assert host.service_path == os.environ.get("MEIVA_SERVICE_PATH")


def test_init_subcommand_binds_its_handler_and_takes_the_identity_flags() -> None:
    """`meiva init` owns the first-start inputs: machine name, capabilities, gateway."""
    args = _parsers.parse_args(
        [
            "init",
            "--machine-name",
            "mac",
            "--serve-gateway",
            "--memory-remote",
            "git@x:y.git",
            "--gateway-url",
            "https://meiva.example.com",
        ]
    )
    assert args.func is _host.h_init
    assert args.machine_name == "mac"
    assert args.serve_gateway is True
    assert args.serve_agent_runner is None  # unset: no flag given
    assert args.memory_remote == "git@x:y.git"
    assert args.gateway_url == "https://meiva.example.com"


def test_maintenance_verbs_opt_out_of_the_gateway_fetch(tmp_path: Path) -> None:
    """`status` / `cluster` / `agents` / `config` / `logs` set MEIVA_CONFIG_FETCH=skip
    before dispatch (settings-lite: they must work while the gateway is down);
    start and normal stop need the real cluster configuration."""
    from base.native_process.root_control import instance_lock
    from base.paths import root_run_dir

    unit = initialized_runner_home(tmp_path.resolve() / "unit", tmp_path / "checkout")

    def dispatch(verb: str) -> dict[str, str]:
        env = {"PATH": "/usr/bin", "MEIVA_HOME": str(unit)}
        _main.opt_into_lite_config([verb], environ=env, home=unit)
        return env

    for verb in ("status", "cluster", "agents", "config", "logs"):
        assert dispatch(verb).get("MEIVA_CONFIG_FETCH") == "skip", f"{verb} must be settings-lite"

    # meiva:note
    # Starting and a stop of a running meiva-root both need data-plane configuration.
    root_lock = instance_lock.acquire_instance_lock(root_run_dir(unit))
    try:
        for verb in ("start", "stop"):
            assert "MEIVA_CONFIG_FETCH" not in dispatch(verb)
    finally:
        instance_lock.release_instance_lock(root_lock)

    # meiva:note
    # A stop with no meiva-root running needs none: repeating it works while the gateway is down.
    assert dispatch("stop").get("MEIVA_CONFIG_FETCH") == "skip"


def test_settings_load_failure_prints_env_template(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When a handler raises ValidationError (fresh host, no .env), main()
    prints a copy-paste env template instead of a raw traceback."""
    from pydantic import ValidationError

    err = ValidationError.from_exception_data(
        "Settings",
        [{"type": "missing", "loc": ("db_url",), "input": None}],  # type: ignore[list-item]  # pyright: ignore[reportArgumentType]
    )

    def _boom(_args: argparse.Namespace) -> int:
        raise err

    args = _main.parse_command_args(["status"])
    args.func = _boom
    rc = _main.dispatch_parsed(
        args,
        ["status"],
        declared=tmp_path,
        checkout=Path(_main.__file__).resolve().parents[1],
        producer_factory=operator_event_pipeline,
        open_database=open_operator_database,
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert "MEIVA_DB_URL" in captured.err


# -- checkout gate: every command from a foreign checkout -----------------------


def _owned_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home that carries its own `source` checkout, named by MEIVA_HOME. This
    checkout is not it, so the gate must refuse every command."""
    home = tmp_path / ".meiva"
    (home / "source").mkdir(parents=True)
    return point_home(monkeypatch, home)


@pytest.mark.parametrize(
    "argv",
    [
        ["init", "--machine-name", "probe", "--serve-gateway"],
        ["start", "--all-services"],
        ["stop", "-y"],
        ["restart"],
        ["converge"],
        ["cluster", "destroy"],
        ["config", "set", "KEY=VALUE"],
        ["logs", "retention"],
        ["agents", "send", "1", "hello"],
        ["status"],
        ["agents", "ls"],
        ["config", "get"],
        ["stop", "--help"],
        ["config", "unset", "--", "--help"],
    ],
)
def test_foreign_checkout_is_refused_before_dispatch(
    argv: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A checkout that is not the home's own does not operate that home: every
    command is refused before anything dispatches, and nothing is written to the
    home."""
    home = _owned_home(tmp_path, monkeypatch)

    rc = _main.main(argv)

    assert rc == 1
    err = capsys.readouterr().err
    assert str(home) in err, "the message must name the home it acts on"
    assert str(home / "source") in err, "and the checkout that may act on it"
    assert "MEIVA_HOME" in err
    assert sorted(path.name for path in home.iterdir()) == ["source"], "nothing may be written"


def test_the_homes_own_checkout_runs_the_state_changing_verbs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The home's `source` IS this checkout (here: a link to it), so its own CLI
    stops its own cluster — the production path."""
    home = tmp_path / ".meiva"
    home.mkdir()
    checkout = Path(_main.__file__).resolve().parents[1]
    (home / "source").symlink_to(checkout)
    point_home(monkeypatch, home)

    assert require_own_checkout(["stop", "-y"], checkout) is None
    assert _main.main(["pty", "status"]) == 0


def test_a_home_with_no_source_accepts_any_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test or scratch home has no checkout of its own: any checkout may
    start, stop and reconfigure it."""
    point_home(monkeypatch, tmp_path)
    checkout = Path(_main.__file__).resolve().parents[1]

    assert require_own_checkout(["stop", "-y"], checkout) is None
    assert _main.main(["pty", "status"]) == 0


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_foreign_checkout_may_still_ask_for_a_lone_help_flag(
    flag: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A lone `-h`/`--help` reaches argparse (which prints and exits 0) rather
    than the gate: no verb runs."""
    _owned_home(tmp_path, monkeypatch)

    with pytest.raises(SystemExit) as exc:
        _main.main([flag])

    assert exc.value.code == 0
    assert "source checkout" not in capsys.readouterr().err


def test_foreign_checkout_may_still_run_bare_meiva(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no argv the gate has no verb to refuse; argparse owns the usage error."""
    _owned_home(tmp_path, monkeypatch)

    with pytest.raises(SystemExit) as exc:
        _main.main([])

    assert exc.value.code == 2
    assert "source checkout" not in capsys.readouterr().err


def _start_entry_probe(scenario: str, tmp_path: Path) -> None:
    """Run the real admitted start entry with the child's service phase boundary."""
    repo = Path(__file__).resolve().parents[2]
    env = {key: value for key, value in os.environ.items() if not key.startswith("MEIVA_")}
    env.update(
        MEIVA_HOME=str(tmp_path / "home"),
        MEIVA_DB_URL="postgresql://foreign.invalid/forbidden",
        MEIVA_GATEWAY_URL="http://foreign.invalid",
    )
    result = subprocess.run(  # noqa: S603 — fixed interpreter and literal probe
        [sys.executable, "-I", "-B", str(_CHILD), scenario, str(repo)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_init_and_start_are_settings_free_until_the_home_is_admitted(tmp_path: Path) -> None:
    """Keep config imports before admission and lazy runtime delivery separate."""
    _start_entry_probe("init_and_start_settings_free", tmp_path)


@pytest.mark.parametrize("scenario", ["warm", "cold", "foreign", "refused", "launcher"])
def test_start_reenters_database_authority_for_its_home(scenario: str, tmp_path: Path) -> None:
    _start_entry_probe(f"start_delivery_{scenario}", tmp_path)


@pytest.mark.parametrize("entry", ["package", "parser", "config"])
def test_command_import_boundary_is_settings_free(entry: str, tmp_path: Path) -> None:
    """`cli.commands` is an empty package door: importing it does no import work
    of its own, so it must load no `cli.commands.*` submodule and pull in no
    `base.config` — Settings stays out of the boundary. The `parser` case
    additionally builds the real argparse tree and parses a real subcommand's
    args (without dispatching to its handler), since `cli.parsers` must stay
    just as settings-free while doing that. The `config` case imports the
    `meiva config` module, whose local repair path must also run without Settings."""
    result = _run_isolated_program("import_boundary", tmp_path, entry)
    assert result.returncode == 0, result.stderr


def test_help_keeps_service_database_admission_unchanged(
    tmp_path: Path,
) -> None:
    """`meiva stop` writes to the database to drain agents, so a host left on stale
    code must still be able to run it through its own handles; help preserves the service gate."""
    result = _run_isolated_program("help_admission", tmp_path)
    assert result.returncode == 0, result.stderr


def test_a_bare_start_is_a_usage_error() -> None:
    # meiva:note
    # Start infers no selection: naming none of the three forms exits 2.
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["start"])
    assert error.value.code == 2


@pytest.mark.parametrize(
    "forms",
    [
        ["--only-service", "gateway", "--all-services"],
        ["--disable-service", "labeler", "--all-services"],
        ["--only-service", "gateway", "--disable-service", "labeler"],
    ],
)
def test_start_selection_forms_are_mutually_exclusive(forms: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["start", *forms])
    assert error.value.code == 2


def test_start_parser_rejects_retired_updater_telemetry() -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["start", "--all-services", "--updater-telemetry"])
    assert error.value.code == 2
