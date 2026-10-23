"""Converge's macOS Application Firewall check — converge the allowlist manifest.

The step has three moves. First, decide precisely whether the host's serving
binaries are allow-listed and name the defect when they are not. Second, prune
stale manifest-family rules so their bundle identifiers are free for re-add.
Third, repair the manifest directly with `socketfilterfw` (exit 0 as the
ordinary user on macOS 15.3.1) and *verify* each rule persisted by re-reading
`--listapps` — the 15.3.1 daemon silently drops an add whose identifier
already has a rule, so an exit-code check would report success for rules that
never existed — and retry through `sudo -n` on releases that still require
elevation.

A plain `sudo` would prompt for a password, and converge runs unattended from
`meiva start`, the watchdog, and the fleet update. A prompt in that path does
not degrade to "unfixed"; it hangs the bring-up, which is strictly worse than the
defect it was trying to repair, and on a headless host it hangs it invisibly.
`sudo -n` either runs the fallback mutation or fails immediately, and a failure
degrades to the historical behavior — print the exact manual commands for the
operator. See `base.host.system.macos_firewall` for the platform compatibility contract.

Because it reports rather than enforces, it warns and returns instead of raising:
a missing firewall rule does not stop this host serving loopback, `meiva start`
is how an operator recovers, and a converge step that blocked the recovery path
over something it cannot fix would be its own outage.
"""

from __future__ import annotations

import contextlib
import sys
from dataclasses import dataclass
from pathlib import Path

from base.cluster.machine import MachineIdentity, MachineNameMissing
from base.host.system import macos_firewall as fw
from base.host.system.hostport import Host
from base.host.system.macos_firewall import FirewallAudit, FirewallRepair, FirewallVerdict
from base.paths import otel_collector_binary
from cli.commands.converge.plans.firewall_report import converge_summary_lines
from cli.commands.converge.spec import ConvergeCtx


def configured_machine_name(machine: MachineIdentity) -> str | None:
    """The configured Meiva machine name (``MEIVA_MACHINE_NAME``), or None when unset."""
    with contextlib.suppress(MachineNameMissing):
        return machine.name()
    return None


def serving_binaries(roles: frozenset[str], *, home: Path) -> tuple[Path, ...]:
    """The binaries this unit serves a non-loopback port from, by capability.

    Resolved through the same helpers that *launch* them, because auditing a path
    the host does not actually run would prove nothing — and the paths are exactly
    where the fragility lives: both are version-stamped, so both orphan their ALF
    rule on a version bump. `sys.executable` is the version-stamped uv interpreter
    (`.../cpython-3.12.11-macos-aarch64-none/bin/python3.12`); `pg_tool` prefers
    the equally version-stamped vendored tree (`~/.meiva/runtime/pg/17.11.0/bin`).

    The interpreter is audited for **either** capability, not just `gateway`. A
    gateway serves the HTTP port every runner dials (issue #949's symptom), and an
    agent-runner serves the ops port the *gateway* dials back — same interpreter,
    same ALF exposure, mirror-image outage. Postgres is gateway-only: a runner
    holds no local data plane, so that binary is absent there. Redis is not audited
    because its server always binds loopback-only and serves no off-box port.

    Filtered to paths that exist, so a resolver falling through to a
    wrong-but-harmless default contributes a phantom "missing rule" instead of a
    real one.
    """
    from base.cluster.dataplane.pg_tools import pg_tool

    candidates = [Path(sys.executable)]
    if "gateway" in roles:
        candidates.append(pg_tool("postgres"))
        # A gateway with a cluster secret serves the collector's authenticated
        # remote OTLP receiver on MEIVA_MACHINE_HOST. Auditing it unconditionally
        # for gateway capability is harmless on a loopback-only single box (the
        # host-level audit exits before reading ALF there) and prevents a hybrid
        # gateway+runner from silently accepting only local telemetry.
        candidates.append(otel_collector_binary(home))
    return tuple(p for p in candidates if p.exists())


def audit_this_host(
    roles: frozenset[str], *, home: Path, machine: MachineIdentity, host: Host
) -> FirewallAudit:
    """Audit this host's own serving binaries. The one entry point every caller shares.

    Converge calls it to report proactively; `meiva firewall status` / `sync` call
    it on demand. Same detector, so they can never disagree about whether the
    firewall is the cause.
    """
    required = serving_binaries(roles, home=home)
    if not required:
        return FirewallAudit(
            FirewallVerdict.ALLOWED, "no serving binary resolved on this host to audit"
        )
    return fw.audit_allowlist(required, machine_host=machine.host(), host=host)


# meiva:note
# What one pass over the allowlist did: the stale rules pruned, the repair, the target size.
@dataclass(frozen=True)
class AllowlistSync:
    pruned: tuple[Path, ...]
    repair: FirewallRepair
    total: int


# meiva:note
# Prune stale manifest-family rules, then allow every missing binary, verifying each.
#
# The audit's own missing set plus the full manifest: the audit resolves the running
# interpreter through the launch helpers (catching a venv the globs do not name), the
# manifest carries the whole declarative whitelist.
#
# Prune before adding: a stale rule still holds its bundle identifier, and macOS 15's ALF
# daemon silently ignores an --add whose identifier already has a rule (see
# base.host.system.macos_firewall). Removing the stale rule first lets the replacement
# version's rule persist in the same pass.
def sync_allowlist(
    audit: FirewallAudit,
    rules: dict[str, bool],
    *,
    machine: MachineIdentity,
    host: Host,
    user_home: Path,
) -> AllowlistSync:
    wanted = {p.resolve() for p in audit.missing}
    wanted.update(
        fw.manifest_paths(
            machine_name=configured_machine_name(machine), host=host, user_home=user_home
        )
    )
    required = tuple(sorted(wanted, key=str))
    pruned = fw.prune_stale_rules(rules, host=host, user_home=user_home)
    if pruned.removed:
        refreshed = fw.allowlisted_paths(host=host)
        if refreshed is not None:
            rules = refreshed
    repair = fw.repair_allowlist(required, rules=rules, host=host)
    return AllowlistSync(pruned.removed, repair, len(required))


def ensure_firewall_allowlist(ctx: ConvergeCtx) -> None:
    """Converge this host's ALF allowlist to the declarative manifest.

    Order of work: diagnose (the audit decides whether this host can even have
    the defect — non-macOS, loopback-only and firewall-off hosts are no-ops),
    then prune stale manifest-family rules (a stale rule still holds its bundle
    identifier, and macOS 15's daemon silently drops an add whose identifier
    already has a rule — removing it first lets the replacement rule persist),
    then repair every missing allow rule directly (with a `sudo -n` fallback),
    verifying each one persisted. A healthy host sees nothing; a host that just
    got repaired sees one line per action; a host where both mutation paths
    failed sees the exact commands, as before. `ctx.roles is None` (a fresh
    install, unit not yet configured) is treated as no capabilities and audits
    the interpreter alone.
    """
    audit = audit_this_host(
        frozenset(ctx.roles or ()), home=ctx.meiva_home, machine=ctx.machine, host=ctx.host
    )
    if audit.verdict is FirewallVerdict.UNREADABLE:
        # Not the defect, and not provably not-the-defect either. Say so once
        # rather than claiming a clean bill of health this step did not establish.
        print(f"  ! firewall: {audit.detail}", file=sys.stderr)
        return
    if audit.verdict not in (FirewallVerdict.ALLOWED, FirewallVerdict.RULES_MISSING):
        return  # not macOS / loopback-only / firewall off — nothing to converge

    rules = fw.allowlisted_paths(host=ctx.host)
    if rules is None:
        print(
            f"  ! firewall: could not read the allow list from {fw.SOCKETFILTERFW}",
            file=sys.stderr,
        )
        return

    outcome = sync_allowlist(
        audit, rules, machine=ctx.machine, host=ctx.host, user_home=Path.home()
    )
    for line in converge_summary_lines(
        removed=len(outcome.pruned),
        allowed=len(outcome.repair.allowed),
        failed=tuple(str(path) for path in outcome.repair.failed),
        total=outcome.total,
        tool=fw.SOCKETFILTERFW,
    ):
        print(line, file=sys.stderr)
