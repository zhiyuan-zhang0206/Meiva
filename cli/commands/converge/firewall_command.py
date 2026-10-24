"""`meiva firewall` — the standalone face of the ALF allowlist manifest.

The converge step (`firewall.ensure_firewall_allowlist`) converges the
allowlist automatically on every source `meiva start`. These verbs give an
operator the same machinery on demand, without a full converge:

- `meiva firewall status` — read-only: verdict, manifest coverage, stale rules,
  and whether the older-macOS sudo fallback grant is installed.
- `meiva firewall sync` — run the rootless-first repair + prune pass now.

Mutation exits 0 as the ordinary user on macOS 15.3.1, but the daemon silently
drops an add whose bundle identifier already has a rule, so `sync` verifies
each rule by re-reading `--listapps` before reporting it. Other releases fall
back to `sudo -n`, then print manual commands if no non-interactive grant is
available.
"""

from __future__ import annotations

import sys
from pathlib import Path

from base.cluster.machine import MachineIdentity
from base.host.system import macos_firewall as fw
from base.host.system.hostport import Host
from cli.commands.converge.firewall import (
    audit_this_host,
    configured_machine_name,
    sync_allowlist,
)
from cli.commands.converge.plans.firewall_report import (
    missing_rules_lines,
    status_tail_lines,
    sync_summary_lines,
)


def cmd_firewall_status(*, home: Path, machine: MachineIdentity, host: Host) -> int:
    """`meiva firewall status` — audit the host and diff the manifest (read-only)."""
    from cli.commands import _repo

    roles = _repo.roles_or_none(machine=machine)
    audit = audit_this_host(frozenset(roles or ()), home=home, machine=machine, host=host)
    print("→ firewall status")
    if audit.verdict is fw.FirewallVerdict.NOT_MACOS:
        print(f"  {audit.verdict.value}: {audit.detail}")
        return 0
    if audit.verdict not in (fw.FirewallVerdict.ALLOWED, fw.FirewallVerdict.RULES_MISSING):
        print(f"  {audit.verdict.value}: {audit.detail}")
    rules = fw.allowlisted_paths(host=host)
    if rules is None:
        print(f"  unreadable: could not read the allow list from {fw.SOCKETFILTERFW}")
        return 1
    print(
        fw.render_manifest_status(
            rules, machine_name=configured_machine_name(machine), host=host, user_home=Path.home()
        )
    )
    stale = tuple(str(path) for path in fw.stale_manifest_rules(rules, user_home=Path.home()))
    for line in status_tail_lines(stale, sudo_grant=fw.sudo_grant_installed(host=host)):
        print(line)
    return 0


def cmd_firewall_sync(*, home: Path, machine: MachineIdentity, host: Host) -> int:
    """`meiva firewall sync` — apply the manifest now (repair + prune).

    Mutates directly first. On older macOS it retries with `sudo -n`; if both
    paths fail, it prints the exact manual commands and exits 1.
    """
    from cli.commands import _repo

    roles = _repo.roles_or_none(machine=machine)
    audit = audit_this_host(frozenset(roles or ()), home=home, machine=machine, host=host)
    if audit.verdict not in (fw.FirewallVerdict.ALLOWED, fw.FirewallVerdict.RULES_MISSING):
        print(f"→ firewall sync: nothing to do ({audit.detail})")
        return 0
    rules = fw.allowlisted_paths(host=host)
    if rules is None:
        print(
            f"  ! firewall: could not read the allow list from {fw.SOCKETFILTERFW}", file=sys.stderr
        )
        return 1
    outcome = sync_allowlist(audit, rules, machine=machine, host=host, user_home=Path.home())
    for line in sync_summary_lines(
        tuple(str(path) for path in outcome.pruned),
        tuple(str(path) for path in outcome.repair.allowed),
        failed=len(outcome.repair.failed),
    ):
        print(line)
    if outcome.repair.failed:
        for line in missing_rules_lines(
            tuple(str(path) for path in outcome.repair.failed), outcome.total, fw.SOCKETFILTERFW
        ):
            print(line, file=sys.stderr)
        return 1
    return 0
