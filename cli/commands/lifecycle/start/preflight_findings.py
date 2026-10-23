# meiva:note
# What the pre-stop start-readiness gate concludes from the facts it read.
#
# Restart checks the coming start's prerequisites while the current services still serve, so a
# local-state refusal leaves the running generation intact. The shell (`readiness_preflight`)
# reads the facts; this module decides which are fatal (they refuse the stop) and which are
# observations (reported, never blocking), words each one, and renders the report. Findings are
# data: a finding never raises.

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "EntrypointFacts",
    "Findings",
    "PortFacts",
    "PortOccupant",
    "PreflightFacts",
    "PreflightReport",
    "TreeFacts",
    "preflight_report",
]


# meiva:note
# A daemon health port another unit's daemon answers on.
@dataclass(frozen=True)
class PortOccupant:
    session: str
    detail: str


# meiva:note
# The health-port gate: whether the roster was resolvable, what raised, who occupies.
@dataclass(frozen=True)
class PortFacts:
    roles_resolved: bool
    check_error: str | None
    occupied: tuple[PortOccupant, ...]


# meiva:note
# One private tree under the home (`logs`, `workspaces`, `memory`) as converge would meet it.
#
# `root_problem` is why the tree root cannot be used (a symlink, not a directory); `skipped`
# are the non-regular nodes inside it that converge skips; `scan_error` is why they could not
# be listed.
@dataclass(frozen=True)
class TreeFacts:
    root: str
    root_problem: str | None
    skipped: tuple[str, ...]
    scan_error: str | None


# meiva:note
# A venv entry point the coming start needs, and what is wrong with it.
@dataclass(frozen=True)
class EntrypointFacts:
    path: str
    label: str
    fix: str
    is_file: bool
    executable: bool


# meiva:note
# Every read-only fact the gate looks at.
#
# `not_directories` are the home's `configs` / `secrets` paths that exist as something mkdir
# cannot tolerate; `marker_problem` is why the metadata marker is not a regular file;
# `migration_error` is the enumeration failure and `unreadable_migrations` the tracked
# migration files the applier could not open, as (name, error).
@dataclass(frozen=True)
class PreflightFacts:
    checkout_problem: str | None
    ports: PortFacts
    trees: tuple[TreeFacts, ...]
    not_directories: tuple[str, ...]
    marker: str
    marker_problem: str | None
    migration_error: str | None
    unreadable_migrations: tuple[tuple[str, str], ...]
    entrypoints: tuple[EntrypointFacts, ...]


# meiva:note
# Fatal findings refuse the stop; observations are reported and the stop proceeds.
@dataclass(frozen=True)
class Findings:
    fatal: tuple[str, ...]
    observations: tuple[str, ...]


# meiva:note
# The gate's verdict and what it prints: stdout lines, stderr lines, 0 proceed / 1 refuse.
@dataclass(frozen=True)
class PreflightReport:
    stdout: tuple[str, ...]
    stderr: tuple[str, ...]
    exit_code: int


# meiva:note
# The blocking pre-bind gate (#977): a daemon health port answered by another home's
# daemon refuses the whole start, and after the stop that refusal has no host left to serve.
# Detection failures are observations: a preflight must never take the host down.
def _port_findings(ports: PortFacts) -> Findings:
    if not ports.roles_resolved:
        return Findings(
            (),
            (
                "machine role did not resolve — port checks skipped (the probe gate "
                "ahead of this one reports the same condition)",
            ),
        )
    if ports.check_error is not None:
        return Findings((), (f"health-port check skipped: {ports.check_error}",))
    fatal = tuple(
        f"{occupant.session}: health port answered by {occupant.detail} — `meiva start` "
        "refuses the whole launch on this (#977); after the stop that refusal leaves "
        "no host serving. Stop the other home's daemon that holds the port"
        for occupant in ports.occupied
    )
    return Findings(fatal, ())


# meiva:note
# Fatal is what converge would raise on: a tree root that is a symlink or not a
# directory, the metadata marker being anything but a regular file, and `configs` /
# `secrets` existing as something mkdir cannot tolerate. Observations are the non-regular
# nodes inside the trees (converge skips them, so they cannot fail a start) and scans that
# could not run.
def _tree_findings(facts: PreflightFacts) -> Findings:
    fatal: list[str] = []
    observations: list[str] = []
    for tree in facts.trees:
        if tree.root_problem is not None:
            fatal.append(
                f"{tree.root} {tree.root_problem} — converge aborts the whole start on "
                "this root; fix the path (or move it aside) so a real directory can be created"
            )
        if tree.scan_error is not None:
            observations.append(
                f"could not scan {tree.root} for non-regular files: {tree.scan_error}"
            )
        observations.extend(
            f"{node} is a socket/FIFO/device — converge skips it (no permission "
            "repair exists); remove it, or stop the process that owns it"
            for node in tree.skipped
        )
    fatal.extend(
        f"{path} exists but is not a directory — converge's mkdir for it "
        "aborts the start; move it aside so a real directory can be created"
        for path in facts.not_directories
    )
    if facts.marker_problem is not None:
        fatal.append(
            f"{facts.marker} {facts.marker_problem} — converge's marker write refuses to "
            "run, aborting the start; replace it with a regular file"
        )
    return Findings(tuple(fatal), tuple(observations))


# meiva:note
# Tracked migration files this checkout cannot read: the applier opens them inside start,
# and the pre-stop layout gate vets names only.
def _migration_findings(facts: PreflightFacts) -> tuple[str, ...]:
    if facts.migration_error is not None:
        return (f"migrations/ cannot be enumerated: {facts.migration_error}",)
    return tuple(
        f"migrations/{name} is not readable ({error}) — `meiva start`'s apply would "
        "fail on the stopped host; fix its mode, or restore the file"
        for name, error in facts.unreadable_migrations
    )


# meiva:note
# Why a venv entry point would fail the coming start: missing, or without the exec bit.
def _entrypoint_findings(entrypoints: tuple[EntrypointFacts, ...]) -> tuple[str, ...]:
    findings: list[str] = []
    for entry in entrypoints:
        if not entry.is_file:
            findings.append(
                f"{entry.path} is missing — {entry.label} would fail after the stop; {entry.fix}"
            )
        elif not entry.executable:
            findings.append(
                f"{entry.path} is not executable — {entry.label} would fail after the "
                f"stop; {entry.fix}"
            )
    return tuple(findings)


# meiva:note
# Decide the gate from `facts`: every finding, worded, with the exit code and what prints.
def preflight_report(facts: PreflightFacts) -> PreflightReport:
    ports = _port_findings(facts.ports)
    trees = _tree_findings(facts)
    checkout = () if facts.checkout_problem is None else (facts.checkout_problem,)
    fatal = (
        *checkout,
        *ports.fatal,
        *trees.fatal,
        *_migration_findings(facts),
        *_entrypoint_findings(facts.entrypoints),
    )
    observations = (*ports.observations, *trees.observations)
    stdout: list[str] = []
    if observations:
        stdout.append("\n→ start-readiness observations (not blocking):")
        stdout.extend(f"  ! {line}" for line in observations)
    if not fatal:
        stdout.append("  ✓ start-readiness preflight passed")
        return PreflightReport(stdout=tuple(stdout), stderr=(), exit_code=0)
    stderr = (
        f"\n✗ start-readiness preflight: {len(fatal)} problem(s) would fail "
        "`meiva start` only after the stop:",
        *(f"    ✗ {line}" for line in fatal),
    )
    return PreflightReport(stdout=tuple(stdout), stderr=stderr, exit_code=1)
