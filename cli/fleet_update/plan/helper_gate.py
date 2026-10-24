# meiva:note
# What the helper state of each host refuses a window on: a host that could not answer (naming
# the fields it owes), a certificate the fleet does not record for that host, a host with no
# baseline, or a loaded helper the target commit would rebuild or re-sign.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md

from __future__ import annotations

from collections.abc import Mapping

from .probe_values import HelperPresence, helper_presence, probe_flag

# meiva:note
# The facts every host must answer for the gate to judge it. A missing, empty or `-` value is how
# a probe that could not run reads, so it refuses instead of passing for "nothing to do".
REQUIRED_FACTS = ("helper", "valid", "loaded", "stale", "cert_root", "expected_hash")

# meiva:note
# Two shapes answer `helper`: a host that carries none (a Linux machine) owes that statement and
# nothing else, because there is nothing there to rebuild or to compare; a host that carries one
# owes every fact.
_ABSENT_OWES = ("helper",)

Probes = Mapping[str, Mapping[str, str]]


def _probe_error(error: str) -> str:
    if error == "archive":
        return "the target commit's archive could not be read"
    if error == "unpack":
        return "the target commit's archive could not be unpacked"
    return f"probe failed ({error})"


def _validate_domains(facts: Mapping[str, str]) -> None:
    presence = facts.get("h_helper", "")
    if presence not in ("", "-"):
        helper_presence(presence)
    for name in ("valid", "loaded", "stale"):
        value = facts.get(f"h_{name}", "")
        if value not in ("", "-"):
            probe_flag(name, value)


# meiva:note
# The fields a host owes and did not answer, led by why the probe itself failed when it did.
def unreadable_facts(facts: Mapping[str, str]) -> tuple[str, ...]:
    _validate_domains(facts)
    owed = _ABSENT_OWES if facts.get("h_helper") == HelperPresence.ABSENT else REQUIRED_FACTS
    bad = [name for name in owed if facts.get(f"h_{name}", "") in ("", "-")]
    if error := facts.get("h_error", ""):
        bad.insert(0, _probe_error(error))
    elif facts.get("h_rc", "") != "0":
        bad.insert(0, f"facts exited {facts.get('h_rc', 'without a status')}")
    return tuple(bad)


# meiva:note
# Only a helper that is there can be rebuilt: `absent` is not a staleness the wave can act on, and
# counting it would retire a host's extras for nothing.
def needs_rebuild(facts: Mapping[str, str]) -> bool:
    _validate_domains(facts)
    if facts.get("h_helper") != "present":
        return False
    return facts.get("h_stale") != "no" or facts.get("h_valid") != "yes"


def _blocked(probes: Probes) -> tuple[str, ...]:
    return tuple(
        alias
        for alias, facts in probes.items()
        if facts.get("h_helper") == "present"
        and facts.get("h_loaded") == "yes"
        and needs_rebuild(facts)
    )


def _baseline_refusals(probes: Probes, table: Mapping[str, str]) -> tuple[str, ...]:
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
    return tuple(reasons)


def _stale_refusals(probes: Probes, table: Mapping[str, str], *, rebuild: bool) -> tuple[str, ...]:
    blocked = _blocked(probes)
    if not blocked or rebuild:
        return ()
    rows = "\n  ".join(
        f"{alias} recorded_hash={facts.get('h_recorded_hash')} "
        f"expected_hash={facts.get('h_expected_hash')} loaded={facts.get('h_loaded')} "
        f"stale={facts.get('h_stale')} valid={facts.get('h_valid')} "
        f"cert_root={facts.get('h_cert_root')} "
        f"vs baseline={table.get(alias, 'MISSING')}"
        for alias, facts in probes.items()
    )
    return (
        f"{len(blocked)} host(s) run a helper this update would have to rebuild, and a loaded "
        f"helper is never replaced in place (it is meiva-root's parent):\n  {rows}"
        "\nTo proceed: accept the rebuild (each rebuilt helper needs its desktop grants "
        "re-applied by hand) and add --rebuild-extras. Not rebuilding: retire extras on those "
        "hosts first.",
    )


# meiva:note
# Every reason the helper state refuses this window.
def refusals(probes: Probes, table: Mapping[str, str], *, rebuild: bool) -> tuple[str, ...]:
    short: dict[str, tuple[str, ...]] = {}
    for alias, facts in probes.items():
        try:
            short[alias] = unreadable_facts(facts)
        except ValueError as exc:
            raise ValueError(f"{alias}: {exc}") from exc
    short = {alias: names for alias, names in short.items() if names}
    reasons: list[str] = []
    if short:
        detail = "; ".join(f"{alias} owes {', '.join(names)}" for alias, names in short.items())
        reasons.append(f"could not read the helper facts of the target commit on: {detail}")
    readable = {alias: facts for alias, facts in probes.items() if alias not in short}
    return (
        *reasons,
        *_baseline_refusals(readable, table),
        *_stale_refusals(readable, table, rebuild=rebuild),
    )


# meiva:note
# The hosts a wave retires extras on so their next start can rebuild the helper: none unless the
# operator accepted the rebuild.
def rebuilding_hosts(probes: Probes, *, rebuild: bool) -> frozenset[str]:
    if not rebuild:
        return frozenset()
    return frozenset(alias for alias, facts in probes.items() if needs_rebuild(facts))
