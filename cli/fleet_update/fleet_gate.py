# meiva:note
# What a wave reads on each host before it stops anything, and what it refuses on: the probe
# script, its parser, the journal reading that says whether a stop finished, and the helper
# gate. Kept beside `cli.fleet_update` because that module is at its file ceiling; like it, this
# one imports no application code, so a window still runs from a bare operator checkout.
# meiva:link cli/docs/fleet-update.meiva.okf.md

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any, Protocol, cast

_HOME = 'H="$HOME/.meiva"; S="$H/source"; cd "$S" || exit 1'

REBUILD_STOP = "--stop-extras --close-shell-sessions --stop-browser --keep-no-services"


class Runner(Protocol):
    # meiva:note
    # What a window step needs from its host channel: run a command (effect or read) and say a
    # line. The gate needs only the first; the verification stage also reports as it goes.
    def run(
        self, alias: str, script: str, *, effect: bool = True, stdin: str | None = None
    ) -> str: ...

    def say(self, line: str) -> None: ...


PROBE = f"""{_HOME}
printf '%s\\n' "head=$(git rev-parse HEAD)"
printf '%s\\n' "dirty=$(git status --porcelain 2>&1 | wc -l | tr -d ' ')"
test -e "$(git rev-parse --git-path hooks/post-checkout)" && echo hook=yes || echo hook=no
test -e "$H/updates/active" && echo active=yes || echo active=no
printf '%s\\n' "op=$(cat "$H/run/lifecycle-op.json" 2>/dev/null)"
"$S/.venv/bin/python" -c 'import fcntl, sys
try:
    fcntl.flock(open(sys.argv[1], "rb"), fcntl.LOCK_SH | fcntl.LOCK_NB)
    print("root=down")
except FileNotFoundError:
    print("root=down")
except OSError:
    print("root=up")' "$H/run/meiva-root/meiva-root.lock" 2>/dev/null"""


def facts_of(text: str) -> dict[str, str]:
    return {key: value for key, _, value in (line.partition("=") for line in text.splitlines())}


def host_refusals(alias: str, facts: dict[str, str]) -> list[str]:
    reasons: list[str] = []
    if facts["dirty"] != "0":
        reasons.append(f"{alias}: $HOME/.meiva/source has uncommitted changes")
    if facts["hook"] == "yes":
        reasons.append(f"{alias}: a post-checkout hook (the guard recurses); move it aside")
    if facts["active"] == "yes":
        reasons.append(
            f"{alias}: $HOME/.meiva/updates/active names an unfinished release operation"
        )
    return reasons


# meiva:note
# The journal a probe line reports; an unreadable record is no journal, never a crash inside the
# gate that is deciding whether to refuse.
def journal_of(text: str) -> dict[str, Any] | None:
    try:
        parsed: Any = json.loads(text)
    except ValueError:
        return None
    return cast("dict[str, Any]", parsed) if isinstance(parsed, dict) else None


# meiva:note
# The host's own journal is the evidence of how its stop ended: a stop that is complete with
# rc 0 succeeded; any other stop record is a failed or unfinished stop. Another operation, or no
# journal, says nothing about a stop.
def stop_completed(journal: dict[str, Any] | None) -> bool:
    if journal is None or journal.get("operation") != "stop":
        return False
    result = cast("dict[str, Any]", journal["result"]) if "result" in journal else {}
    return journal.get("complete") is True and result.get("rc") == 0


def failed_stop(journal: dict[str, Any] | None) -> bool:
    return (
        journal is not None and journal.get("operation") == "stop" and not stop_completed(journal)
    )


# meiva:note
# One host's probe facts, and whether it was serving when the window began: a host is already
# stopped only when its last lifecycle operation is a completed, successful stop and its
# meiva-root is not running; any other host was up.
def probe_host(s: Runner, alias: str) -> tuple[dict[str, str], bool]:
    facts = facts_of(s.run(alias, PROBE, effect=False))
    stopped = facts.get("root") == "down" and stop_completed(journal_of(facts.get("op", "")))
    return facts, not stopped


# meiva:note
# The helper facts of the commit the wave switches to, read by that commit's own module: the
# script and the package it imports are extracted from NEW's objects, because the installed
# checkout is the OLD commit and on a first wave does not carry this command at all.
def probe_helper(s: Runner, alias: str, new: str) -> dict[str, str]:
    script = (
        f"{_HOME}\n"
        "tmp=$(mktemp -d)\n"
        f'git -C "$S" archive {shlex.quote(new)} | tar -x -C "$tmp"\n'
        'out=$(cd "$tmp" && PYTHONPATH="$tmp" "$S/.venv/bin/python" '
        '"$tmp/scripts/host_ops/tcc/helper-signing.py" facts 2>&1); rc=$?\n'
        'echo "h_rc=$rc"\n'
        "printf '%s\\n' \"$out\" | sed 's/^/h_/'\n"
        'rm -rf "$tmp"'
    )
    return facts_of(s.run(alias, script, effect=False))


# meiva:note
# The facts every host must answer for the gate to judge it. A missing, empty or `-` value is
# how a probe that could not run reads, so it refuses instead of passing for "nothing to do".
REQUIRED_FACTS = ("helper", "valid", "loaded", "stale", "cert_root", "expected_hash")


# meiva:note
# Two shapes answer `helper`: a host that carries none (a Linux machine) owes that statement and
# nothing else, because there is nothing there to rebuild or to compare; a host that carries one
# owes every fact. A missing, empty or `-` value is how a probe that could not run reads, so any
# owed field that comes back that way refuses the window instead of passing for "nothing to do".
_ABSENT_OWES = ("helper",)

_PROBE_ERRORS = {
    "archive": "the target commit's archive could not be read",
    "unpack": "the target commit's archive could not be unpacked",
}


def unreadable_facts(facts: dict[str, str]) -> list[str]:
    owed = _ABSENT_OWES if facts.get("h_helper") == "absent" else REQUIRED_FACTS
    bad = [name for name in owed if facts.get(f"h_{name}", "") in ("", "-")]
    if error := facts.get("h_error", ""):
        bad.insert(0, _PROBE_ERRORS.get(error, f"probe failed ({error})"))
    elif facts.get("h_rc", "") != "0":
        bad.insert(0, f"facts exited {facts.get('h_rc', 'without a status')}")
    return bad


# meiva:note
# Only a helper that is there can be rebuilt: `absent` is not a staleness the wave can act on,
# and counting it would retire a host's extras for nothing.
def needs_rebuild(facts: dict[str, str]) -> bool:
    if facts.get("h_helper") != "present":
        return False
    return facts.get("h_stale") != "no" or facts.get("h_valid") != "yes"


# meiva:note
# The identity this fleet records for each host, read from the tracked table at call time: the
# table is data a pull request changes, never something a window writes.
def baselines(repo: Path) -> dict[str, str]:
    return json.loads((repo / "deploy" / "fleet" / "helper-identities.json").read_text())


def _baseline_refusals(probes: dict[str, dict[str, str]], table: dict[str, str]) -> list[str]:
    reasons: list[str] = []
    for alias, facts in probes.items():
        if facts.get("h_helper") != "present":
            continue
        baseline = table.get(alias)
        if baseline is None:
            reasons.append(
                f"{alias}: no helper identity baseline recorded; add this host to "
                "deploy/fleet/helper-identities.json in a PR naming the change"
            )
        elif facts.get("h_cert_root") != baseline:
            reasons.append(
                f"{alias}: helper certificate root {facts.get('h_cert_root')} is not the "
                f"recorded baseline {baseline}"
            )
    return reasons


def _stale_refusals(
    probes: dict[str, dict[str, str]], table: dict[str, str], *, rebuild: bool
) -> list[str]:
    blocked = _blocked(probes)
    if not blocked or rebuild:
        return []
    rows = "\n  ".join(
        f"{alias} recorded_hash={facts.get('h_recorded_hash')} "
        f"expected_hash={facts.get('h_expected_hash')} loaded={facts.get('h_loaded')} "
        f"stale={facts.get('h_stale')} valid={facts.get('h_valid')} "
        f"cert_root={facts.get('h_cert_root')} "
        f"vs baseline={table.get(alias, 'MISSING')}"
        for alias, facts in probes.items()
    )
    return [
        f"{len(blocked)} host(s) run a helper this update would have to rebuild, and a loaded "
        f"helper is never replaced in place (it is meiva-root's parent):\n  {rows}"
        "\nTo proceed: accept the rebuild (each rebuilt helper needs its desktop grants "
        "re-applied by hand) and add --rebuild-extras. Not rebuilding: retire extras on those "
        "hosts first."
    ]


# meiva:note
# Every reason the helper state refuses this window: a host that could not answer (naming the
# fields it owes), a certificate the fleet does not record for that host, a host with no
# baseline, or a loaded helper the target commit would rebuild or re-sign.
def refusals(
    probes: dict[str, dict[str, str]], table: dict[str, str], *, rebuild: bool
) -> list[str]:
    short = {alias: unreadable_facts(facts) for alias, facts in probes.items()}
    short = {alias: names for alias, names in short.items() if names}
    reasons: list[str] = []
    if short:
        detail = "; ".join(f"{alias} owes {', '.join(names)}" for alias, names in short.items())
        reasons.append(f"could not read the helper facts of the target commit on: {detail}")
    readable = {a: p for a, p in probes.items() if a not in short}
    return [
        *reasons,
        *_baseline_refusals(readable, table),
        *_stale_refusals(readable, table, rebuild=rebuild),
    ]


def rebuilding_hosts(probes: dict[str, dict[str, str]], *, rebuild: bool) -> set[str]:
    if not rebuild:
        return set()
    return {alias for alias, facts in probes.items() if needs_rebuild(facts)}


def _blocked(probes: dict[str, dict[str, str]]) -> list[str]:
    return [
        alias
        for alias, facts in probes.items()
        if facts.get("h_helper") == "present"
        and facts.get("h_loaded") == "yes"
        and needs_rebuild(facts)
    ]
