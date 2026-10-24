# meiva:note
# The converge firewall step and the `meiva firewall` verbs.
#
# The audit's own decision table is pinned in `base/host/system/tests/test_macos_firewall.py`.
# What is asserted here is the step's jobs on top of it: which serving binaries it audits per
# capability, what it says and does about the answer, and that it reports rather than raises.
#
# **Nothing here touches a real firewall.** `socketfilterfw` is a macOS system tool, so each test
# hands the step a host whose process runner is `AlfDaemon`, a stand-in for the Application
# Firewall daemon that keeps the rule list the real one keeps. Whatever the machine running the
# suite has installed is declared allowed up front (`_daemon`), so only the binaries a test names
# can be missing, and the user's home is the fake OS user's (`default_home`).

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

import cli.commands.converge.firewall as cfw
import cli.commands.converge.firewall_command as firewall_cmd
import cli.commands.lifecycle.converge_host as cv
from base.cluster.dataplane import runtime_binaries as rb
from base.cluster.machine import MachineIdentity
from base.host.system import macos_firewall as fw
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import DARWIN_ARM64, LINUX_X86, fake_host
from base.paths import otel_collector_binary
from cli.commands.converge.plans.firewall_report import (
    converge_summary_lines,
    missing_rules_lines,
    status_tail_lines,
    sync_summary_lines,
)
from cli.commands.converge.spec import ALL_ROLES, ConvergeCtx
from cli.database.runtime import CommandRuntime

_OFF_BOX = "10.0.0.5"
# meiva:note
# A runner audits the interpreter only; a gateway would also resolve Postgres (`brew` on a Mac).
_RUNNER = frozenset({"agent-runner"})
_TOOL = fw.SOCKETFILTERFW


# meiva:note
# The Application Firewall daemon as `Host.run` reaches it: one `socketfilterfw` rule list.
#
# It answers the queries the audit makes (`--getglobalstate`, `--listapps`) and applies the
# mutations the repair makes (`--add`, `--unblockapp`, `--remove`), directly or through
# `sudo -n`. `refuses_mutations` models a daemon that rejects both paths; `shadowed_by` models
# macOS 15's bundle-identifier dedup: an `--add` is accepted and persists nothing while that
# rule exists.
class AlfDaemon:
    def __init__(
        self,
        rules: dict[str, bool],
        *,
        enabled: bool = True,
        readable: bool = True,
        refuses_mutations: bool = False,
        shadowed_by: str | None = None,
        sudo_grant: bool = False,
    ) -> None:
        self.rules = dict(rules)
        self.enabled = enabled
        self.readable = readable
        self.refuses_mutations = refuses_mutations
        self.shadowed_by = shadowed_by
        self.sudo_grant = sudo_grant
        self.mutation_attempts: list[list[str]] = []

    def __call__(
        self, argv: Sequence[str], *, timeout: float, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        command = list(argv)
        if command == ["sudo", "-n", "-l"]:
            if self.sudo_grant:
                return self._reply(command, "MEIVA_FIREWALL\n")
            return self._reply(command, "", rc=1)
        if command[:2] == ["scutil", "--get"]:
            return self._reply(command, "", rc=1)
        verbs = command[2:] if command[:2] == ["sudo", "-n"] else command
        if verbs[0] != _TOOL:
            raise AssertionError(f"unscripted command: {command}")
        verb, *rest = verbs[1:]
        if verb in ("--getglobalstate", "--listapps"):
            return self._query(command, verb)
        self.mutation_attempts.append(command)
        if self.refuses_mutations:
            return self._reply(command, "", rc=1)
        self._mutate(verb, rest[0])
        return self._reply(command, "", rc=0)

    def _query(self, command: list[str], verb: str) -> subprocess.CompletedProcess[str]:
        if not self.readable:
            return self._reply(command, "", rc=1)
        if verb == "--getglobalstate":
            word, state = ("enabled", 1) if self.enabled else ("disabled", 0)
            return self._reply(command, f"Firewall is {word}. (State = {state})\n")
        lines = [f"Total number of apps = {len(self.rules)}", ""]
        for index, (path, allowed) in enumerate(self.rules.items(), start=1):
            lines.append(f"{index} : {path}")
            lines.append(f"             ({'Allow' if allowed else 'Block'} incoming connections)")
        return self._reply(command, "\n".join(lines) + "\n")

    def _mutate(self, verb: str, path: str) -> None:
        if verb == "--remove":
            self.rules.pop(path, None)
        elif verb == "--add":
            if self.shadowed_by is None or self.shadowed_by not in self.rules:
                self.rules[path] = True
        elif verb == "--unblockapp":
            if path in self.rules:
                self.rules[path] = True
        else:
            raise AssertionError(f"unscripted socketfilterfw verb: {verb}")

    @staticmethod
    def _reply(command: list[str], stdout: str, *, rc: int = 0) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, rc, stdout, "")


def _host_with(daemon: AlfDaemon) -> Host:
    return fake_host(daemon, platform=DARWIN_ARM64, programs={_TOOL: _TOOL})


# meiva:note
# Every manifest binary this machine happens to have installed.
def _installed_here() -> tuple[Path, ...]:
    return fw.manifest_paths(
        machine_name=None, host=_host_with(AlfDaemon({})), user_home=Path.home()
    )


# meiva:note
# A daemon that already allows what is installed here, plus `allowed`.
def _daemon(
    *,
    allowed: tuple[Path, ...] = (),
    enabled: bool = True,
    readable: bool = True,
    refuses_mutations: bool = False,
    shadowed_by: str | None = None,
    sudo_grant: bool = False,
) -> AlfDaemon:
    rules = {str(path): True for path in (*_installed_here(), *allowed)}
    return AlfDaemon(
        rules,
        enabled=enabled,
        readable=readable,
        refuses_mutations=refuses_mutations,
        shadowed_by=shadowed_by,
        sudo_grant=sudo_grant,
    )


# meiva:note
# A context on a mac whose Application Firewall is `daemon` and whose address is off-box.
def _firewall_ctx(
    ctx: ConvergeCtx, daemon: AlfDaemon, *, roles: frozenset[str] | None, home: Path
) -> ConvergeCtx:
    ctx.machine.set(host=_OFF_BOX)
    return replace(ctx, host=_host_with(daemon), roles=roles, meiva_home=home)


def _interpreter() -> Path:
    return Path(sys.executable).resolve()


# meiva:note
# A vendored Postgres under the fake user's default home, where `pg_tool` looks first.
def _seed_vendored_postgres(user_home: Path) -> Path:
    bin_dir = rb.vendored_pg_dir() / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "initdb").write_text("#!/bin/sh\n")
    postgres = bin_dir / "postgres"
    postgres.write_text("#!/bin/sh\n")
    return postgres


# meiva:note
# --- what is reported (pure) --------------------------------------------------------


def test_a_sync_reports_each_action_and_nothing_when_nothing_happened() -> None:
    assert converge_summary_lines(removed=0, allowed=0, failed=(), total=3, tool=_TOOL) == ()
    assert converge_summary_lines(removed=2, allowed=1, failed=(), total=3, tool=_TOOL) == (
        "  · firewall: removed 2 stale allow rules",
        "  · firewall: allowed 1 binaries (no popup on their next bind)",
    )


def test_the_manual_fallback_names_every_binary_and_the_exact_commands() -> None:
    lines = missing_rules_lines(("/opt/a/bin/listener", "/opt/b/bin/listener"), 5, _TOOL)

    assert lines[0].startswith("  ! firewall: 2 of 5 managed binaries have no ALF allow rule")
    assert "    - no allow rule: /opt/a/bin/listener" in lines
    assert "    - no allow rule: /opt/b/bin/listener" in lines
    assert f'      sudo "{_TOOL}" --add "/opt/a/bin/listener"' in lines
    assert f'      sudo "{_TOOL}" --unblockapp "/opt/b/bin/listener"' in lines
    assert "off-box peers cannot reach these services" in " ".join(lines)
    assert "older macOS" in " ".join(lines)
    # meiva:note
    # an already-bound socket keeps its old policy
    assert "re-bind" in lines[-1]


def test_the_converge_report_ends_with_the_manual_fallback_when_a_repair_failed() -> None:
    lines = converge_summary_lines(removed=0, allowed=0, failed=("/uv/a",), total=1, tool=_TOOL)

    assert lines == missing_rules_lines(("/uv/a",), 1, _TOOL)


def test_the_sync_verb_lists_each_rule_and_says_when_nothing_changed() -> None:
    assert sync_summary_lines(("/old",), ("/new",), failed=0) == (
        "  · removed stale rule /old",
        "  · allowed /new",
    )
    assert sync_summary_lines((), (), failed=0) == ("  already converged — nothing to do",)
    assert sync_summary_lines((), (), failed=1) == ()


def test_the_status_tail_lists_stale_rules_and_the_sudo_grant() -> None:
    stale = status_tail_lines(("/old/node",), sudo_grant=False)
    granted = status_tail_lines((), sudo_grant=True)

    assert stale[:2] == ("  stale rules: 1", "    - /old/node")
    assert "not installed" in stale[-1]
    assert granted == (
        "  stale rules: 0",
        "  sudo fallback grant: installed (used only if direct mutation is rejected)",
    )


# meiva:note
# --- which binaries the step audits -------------------------------------------------


def test_step_is_registered_in_converge_for_both_capabilities() -> None:
    """A gateway serves the HTTP port; a runner serves the ops port the gateway dials.

    Both are the same version-stamped interpreter behind the same ALF exposure, so
    role-scoping this to `gateway` would leave the mirror-image outage undiagnosed.
    """
    step = next(s for s in cv.CONVERGE_STEPS if s.apply is cfw.ensure_firewall_allowlist)
    assert step.roles == ALL_ROLES
    # No unit config needed: the audit reads the host, so a fresh install can run it.
    assert step.requires_unit_config is False
    assert step.host_global is False


def test_runner_audits_only_the_interpreter(tmp_path: Path) -> None:
    """An agent-runner holds no local data plane, so its daemons need no audit."""
    assert cfw.serving_binaries(frozenset({"agent-runner"}), home=tmp_path) == (
        Path(sys.executable),
    )


def test_gateway_audits_the_data_plane_too(default_home: Path) -> None:
    """Postgres and the remote OTLP receiver bind this gateway's off-box address."""
    home = default_home / ".meiva"
    postgres = _seed_vendored_postgres(default_home)
    otelcol = otel_collector_binary(home)
    otelcol.parent.mkdir(parents=True)
    otelcol.write_text("#!/bin/sh\n")

    assert cfw.serving_binaries(frozenset({"gateway"}), home=home) == (
        Path(sys.executable),
        postgres,
        otelcol,
    )


# meiva:note
# A resolver can fall back to a path that is absent on this host.
#
# Auditing that phantom path would manufacture a permanent "missing rule" on every host
# without it, so only paths that exist are audited.
def test_a_binary_that_is_not_installed_is_not_audited(default_home: Path) -> None:
    home = default_home / ".meiva"
    postgres = _seed_vendored_postgres(default_home)

    # meiva:note
    # No collector under this home: its binary path resolves to nothing that exists.
    assert cfw.serving_binaries(frozenset({"gateway"}), home=home) == (
        Path(sys.executable),
        postgres,
    )


# meiva:note
# --- the step: reporting rather than raising ----------------------------------------


@pytest.mark.parametrize(
    "cannot_have_the_defect", ["not-macos", "loopback-only", "firewall-off", "allowed"]
)
def test_quiet_on_every_host_that_cannot_have_the_defect(
    cannot_have_the_defect: str,
    ctx: ConvergeCtx,
    default_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Silence is the contract for a healthy host: converge output an operator
    learns to skim is worthless for finding the one host that is broken."""
    daemon = _daemon(allowed=(_interpreter(),), enabled=cannot_have_the_defect != "firewall-off")
    host = _firewall_ctx(ctx, daemon, roles=_RUNNER, home=default_home / ".meiva")
    if cannot_have_the_defect == "not-macos":
        host = replace(host, host=fake_host(daemon, platform=LINUX_X86))
    if cannot_have_the_defect == "loopback-only":
        host.machine.set(host="127.0.0.1")

    cfw.ensure_firewall_allowlist(host)

    assert capsys.readouterr().err == ""
    assert daemon.mutation_attempts == []


def test_unreadable_says_so_instead_of_claiming_healthy(
    ctx: ConvergeCtx, default_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Silence would assert a clean bill of health the step did not establish."""
    daemon = _daemon(readable=False)

    cfw.ensure_firewall_allowlist(
        _firewall_ctx(ctx, daemon, roles=_RUNNER, home=default_home / ".meiva")
    )

    err = capsys.readouterr().err
    assert "could not read the firewall state" in err
    assert "--add" not in err  # no repair is offered for an unknown state
    assert daemon.mutation_attempts == []


# meiva:note
# Direct mutation and the old-macOS fallback both fail, so the historical
# manual commands are printed without raising or blocking recovery.
def test_failed_direct_and_sudo_repairs_print_exact_commands_and_do_not_raise(
    ctx: ConvergeCtx, default_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = _interpreter()
    daemon = _daemon(refuses_mutations=True)
    # meiva:note
    # the manifest, plus the interpreter that has no rule
    managed = len(_installed_here()) + 1

    cfw.ensure_firewall_allowlist(
        _firewall_ctx(ctx, daemon, roles=_RUNNER, home=default_home / ".meiva")
    )  # no raise

    err = capsys.readouterr().err
    assert f"1 of {managed} managed binaries have no ALF allow rule" in err
    assert str(missing) in err
    assert "--add" in err and "--unblockapp" in err
    assert daemon.mutation_attempts == [
        [_TOOL, "--add", str(missing)],
        ["sudo", "-n", _TOOL, "--add", str(missing)],
    ]
    assert "older macOS" in err
    # meiva:note
    # the rule alone is not enough: a bound socket keeps its policy
    assert "re-bind" in err
    assert "off-box peers cannot reach these services" in err


# meiva:note
# The missing rule is fixed in place: one line, no error block, no commands to
# paste. The claim is only made after a re-read of `--listapps` confirms the rule persisted.
def test_a_missing_rule_is_repaired_in_one_line(
    ctx: ConvergeCtx, default_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    daemon = _daemon()

    cfw.ensure_firewall_allowlist(
        _firewall_ctx(ctx, daemon, roles=_RUNNER, home=default_home / ".meiva")
    )

    err = capsys.readouterr().err
    assert "allowed 1 binaries" in err
    # meiva:note
    # no manual commands on the repaired path
    assert "--add" not in err
    assert daemon.rules[str(_interpreter())] is True


# meiva:note
# A version bump left an orphaned rule; it is removed.
def test_stale_rules_are_pruned_when_the_daemon_accepts_the_removal(
    ctx: ConvergeCtx, default_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stale = (
        default_home / ".local/share/uv/python/cpython-3.12.11-macos-aarch64-none/bin/python3.12"
    )
    daemon = _daemon(allowed=(_interpreter(), stale))

    cfw.ensure_firewall_allowlist(
        _firewall_ctx(ctx, daemon, roles=_RUNNER, home=default_home / ".meiva")
    )

    assert "removed 1 stale allow rules" in capsys.readouterr().err
    assert str(stale) not in daemon.rules


# meiva:note
# macOS 15's daemon drops an add whose bundle identifier already has a
# rule, so a stale rule must be pruned before the replacement version is
# added — in the same pass, not on the next converge.
#
# The daemon models the dedup: an `--add` is a silent no-op while the stale rule for the same
# identifier exists, and only persists after that rule is removed. The repair succeeding at all
# therefore proves prune ran first; had the step repaired before pruning, the add would be
# dropped and the run would report the failure instead.
def test_prune_runs_before_repair_so_replacement_rules_persist(
    ctx: ConvergeCtx, default_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stale = (
        default_home / ".local/share/uv/python/cpython-3.12.12-macos-aarch64-none/bin/python3.12"
    )
    daemon = _daemon(allowed=(stale,), shadowed_by=str(stale))

    cfw.ensure_firewall_allowlist(
        _firewall_ctx(ctx, daemon, roles=_RUNNER, home=default_home / ".meiva")
    )

    err = capsys.readouterr().err
    assert "allowed 1 binaries" in err  # the add persisted only because prune ran first
    assert "removed 1 stale allow rules" in err
    assert daemon.rules[str(_interpreter())] is True


# meiva:note
# `roles is None` is a fresh install — no capabilities, but still an interpreter.
def test_an_unconfigured_unit_audits_the_interpreter_alone(
    ctx: ConvergeCtx, default_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    daemon = _daemon()

    cfw.ensure_firewall_allowlist(
        _firewall_ctx(ctx, daemon, roles=None, home=default_home / ".meiva")
    )

    assert (
        "allowed 1 binaries" in capsys.readouterr().err
        # meiva:note
        # the interpreter; no Postgres, no collector
    )


# meiva:note
# --- the verbs ---------------------------------------------------------------------


# meiva:note
# The machine (off-box) and the mac whose firewall is `daemon`, as the verbs take them.
def _verb_host(command_runtime: CommandRuntime, daemon: AlfDaemon) -> tuple[MachineIdentity, Host]:
    command_runtime.machine.set(host=_OFF_BOX)
    return command_runtime.machine, _host_with(daemon)


def test_firewall_status_renders_manifest_details(
    ctx: ConvergeCtx,
    command_runtime: CommandRuntime,
    default_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stale = (
        default_home / ".local/share/uv/python/cpython-3.12.11-macos-aarch64-none/bin/python3.12"
    )
    daemon = _daemon(allowed=(_interpreter(), stale), sudo_grant=True)

    machine, host = _verb_host(command_runtime, daemon)

    rc = firewall_cmd.cmd_firewall_status(
        home=default_home / ".meiva", user_home=default_home, machine=machine, host=host
    )

    output = capsys.readouterr().out
    assert rc == 0
    assert output.startswith("→ firewall status\n")
    assert "uv interpreter" in output
    assert "  Summary: " in output
    assert "  stale rules: 1" in output and f"    - {stale}" in output
    assert "sudo fallback grant: installed" in output


def test_firewall_status_without_the_grant_says_so(
    ctx: ConvergeCtx,
    command_runtime: CommandRuntime,
    default_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    daemon = _daemon(allowed=(_interpreter(),))

    machine, host = _verb_host(command_runtime, daemon)

    rc = firewall_cmd.cmd_firewall_status(
        home=default_home / ".meiva", user_home=default_home, machine=machine, host=host
    )

    assert rc == 0
    assert "sudo fallback grant: not installed" in capsys.readouterr().out


def test_firewall_status_off_macos_is_one_line(
    command_runtime: CommandRuntime, default_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = firewall_cmd.cmd_firewall_status(
        home=default_home / ".meiva",
        user_home=default_home,
        machine=command_runtime.machine,
        host=fake_host(platform=LINUX_X86),
    )

    assert rc == 0
    assert capsys.readouterr().out == "→ firewall status\n  not_macos: not a macOS host\n"


def test_firewall_status_fails_when_the_allow_list_cannot_be_read(
    command_runtime: CommandRuntime, default_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    command_runtime.machine.set(host=_OFF_BOX)
    daemon = _daemon(allowed=(_interpreter(),))
    daemon.readable = False

    rc = firewall_cmd.cmd_firewall_status(
        home=default_home / ".meiva",
        user_home=default_home,
        machine=command_runtime.machine,
        host=_host_with(daemon),
    )

    assert rc == 1
    assert "unreadable: could not read the allow list" in capsys.readouterr().out


def test_firewall_sync_allows_what_is_missing(
    ctx: ConvergeCtx,
    command_runtime: CommandRuntime,
    default_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    daemon = _daemon()

    machine, host = _verb_host(command_runtime, daemon)

    rc = firewall_cmd.cmd_firewall_sync(
        home=default_home / ".meiva", user_home=default_home, machine=machine, host=host
    )

    assert rc == 0
    assert capsys.readouterr().out == f"  · allowed {_interpreter()}\n"
    assert daemon.rules[str(_interpreter())] is True


def test_firewall_sync_of_a_converged_host_changes_nothing(
    ctx: ConvergeCtx,
    command_runtime: CommandRuntime,
    default_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    daemon = _daemon(allowed=(_interpreter(),))

    machine, host = _verb_host(command_runtime, daemon)

    rc = firewall_cmd.cmd_firewall_sync(
        home=default_home / ".meiva", user_home=default_home, machine=machine, host=host
    )

    assert rc == 0
    assert capsys.readouterr().out == "  already converged — nothing to do\n"
    assert daemon.mutation_attempts == []


def test_firewall_sync_prints_the_manual_commands_and_fails_when_both_paths_fail(
    ctx: ConvergeCtx,
    command_runtime: CommandRuntime,
    default_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    daemon = _daemon(refuses_mutations=True)

    machine, host = _verb_host(command_runtime, daemon)

    rc = firewall_cmd.cmd_firewall_sync(
        home=default_home / ".meiva", user_home=default_home, machine=machine, host=host
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert f'sudo "{_TOOL}" --add "{_interpreter()}"' in captured.err


def test_firewall_sync_off_macos_has_nothing_to_do(
    command_runtime: CommandRuntime, default_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = firewall_cmd.cmd_firewall_sync(
        home=default_home / ".meiva",
        user_home=default_home,
        machine=command_runtime.machine,
        host=fake_host(platform=LINUX_X86),
    )

    assert rc == 0
    assert capsys.readouterr().out == "→ firewall sync: nothing to do (not a macOS host)\n"
