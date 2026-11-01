# meiva:note
# What a window refuses on, from what the hosts' probes printed: the host facts, the helper facts
# of the target commit and the OLD commit the hosts share. The probes' own text is the input and
# the refusal is the value, so a gate that read the wrong field fails here without a host.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md
from __future__ import annotations

import pytest

from cli.fleet_update.plan.helper_gate import (
    needs_rebuild,
    rebuilding_hosts,
    refusals,
    unreadable_facts,
)
from cli.fleet_update.plan.host_probe import (
    facts_of,
    host_refusals,
    supports_stop_message,
    was_serving,
)
from cli.fleet_update.plan.old_target import (
    OldTarget,
    old_commit,
    python_change_reason,
    unfetched_old_reason,
)

_STOP_OK = '{"operation": "stop", "complete": true, "result": {"rc": 0}}'


@pytest.mark.parametrize(
    ("journal", "root", "stopped"),
    [
        (_STOP_OK, "down", True),
        (_STOP_OK, "up", False),
        ('{"operation": "stop", "complete": true, "result": {"rc": 1}}', "down", False),
        ('{"operation": "stop", "complete": false}', "down", False),
        ('{"operation": "restart", "complete": true, "result": {"rc": 0}}', "down", False),
        ("", "down", False),
        ("{not json", "down", False),
        (_STOP_OK, "", False),
    ],
)
def test_a_host_is_already_stopped_only_when_its_last_operation_is_a_completed_stop_and_no_root_runs(
    journal: str, root: str, stopped: bool
) -> None:
    probe = f"head=abc\nop={journal}\n" + (f"root={root}\n" if root else "")

    assert was_serving(facts_of(probe)) is not stopped


def test_the_probe_lines_become_facts_and_a_value_may_carry_equals_signs() -> None:
    facts = facts_of('head=abc\nop={"a": "b=c"}\nbare\n')
    assert facts == {"head": "abc", "op": '{"a": "b=c"}', "bare": ""}


_CLEAN = {"head": "abc", "dirty": "0", "hook": "no", "active": "no"}


def test_a_clean_host_has_nothing_to_refuse() -> None:
    assert host_refusals("lin", _CLEAN) == ()


@pytest.mark.parametrize(
    ("fact", "reason"),
    [
        ({"dirty": "3"}, "lin: $HOME/.meiva/source has uncommitted changes"),
        ({"hook": "yes"}, "lin: a post-checkout hook (the guard recurses); move it aside"),
        (
            {"active": "yes"},
            "lin: $HOME/.meiva/updates/active names an unfinished release operation",
        ),
    ],
)
def test_each_unsafe_host_fact_is_its_own_refusal(fact: dict[str, str], reason: str) -> None:
    assert host_refusals("lin", {**_CLEAN, **fact}) == (reason,)


def test_every_unsafe_fact_is_named_at_once() -> None:
    facts = {**_CLEAN, "dirty": "1", "hook": "yes", "active": "yes"}
    assert len(host_refusals("lin", facts)) == 3


def test_a_probe_that_did_not_run_to_its_end_refuses_naming_what_is_missing() -> None:
    assert host_refusals("lin", {"head": "abc", "dirty": "0", "hook": "no"}) == (
        "lin: the probe did not report active",
    )
    assert len(host_refusals("lin", {})) == 4


def test_hosts_that_share_one_other_commit_have_that_old() -> None:
    heads = {"gw": "aaa", "mac": "aaa", "lin": "aaa"}
    assert old_commit(heads, "bbb") == OldTarget((), "aaa")


def test_a_fleet_already_on_new_has_no_old_and_is_not_refused() -> None:
    assert old_commit({"gw": "bbb", "lin": "bbb"}, "bbb") == OldTarget((), None)


def test_hosts_on_different_older_commits_are_refused_naming_the_heads() -> None:
    heads = {"gw": "aaa", "mac": "ccc"}
    assert old_commit(heads, "bbb") == OldTarget((f"hosts disagree on HEAD: {heads}",), None)


def test_a_host_already_on_new_does_not_disagree_with_the_rest() -> None:
    assert old_commit({"gw": "aaa", "mac": "bbb"}, "bbb") == OldTarget((), "aaa")


def test_an_old_the_operators_checkout_lacks_is_a_refusal_naming_the_repository() -> None:
    assert (
        unfetched_old_reason("aaa", "/ops/repo") == "OLD aaa is not in /ops/repo: git fetch origin"
    )


@pytest.mark.parametrize(
    ("changed", "allowed", "reasons"),
    [
        (False, False, ()),
        (False, True, ()),
        (True, True, ()),
        (True, False, (".python-version changes (venvs rebuild): pass --allow-python-change",)),
    ],
)
def test_a_python_change_needs_the_operators_flag(
    changed: bool, allowed: bool, reasons: tuple[str, ...]
) -> None:
    assert python_change_reason(changed=changed, allowed=allowed) == reasons


_PRESENT = {
    "h_rc": "0",
    "h_helper": "present",
    "h_valid": "yes",
    "h_loaded": "no",
    "h_stale": "no",
    "h_cert_root": "f" * 40,
    "h_expected_hash": "a" * 64,
    "h_recorded_hash": "a" * 64,
}
_TABLE = {"mac": "f" * 40, "lin": "f" * 40}


def test_a_host_that_answers_in_full_owes_nothing() -> None:
    assert unreadable_facts(_PRESENT) == ()


def test_a_host_with_no_helper_owes_only_that_statement() -> None:
    assert unreadable_facts({"h_rc": "0", "h_helper": "absent"}) == ()


@pytest.mark.parametrize(
    "dropped", ["helper", "valid", "loaded", "stale", "cert_root", "expected_hash"]
)
def test_a_missing_or_dash_fact_is_owed(dropped: str) -> None:
    without = {k: v for k, v in _PRESENT.items() if k != f"h_{dropped}"}
    assert unreadable_facts(without) == (dropped,)
    assert unreadable_facts({**_PRESENT, f"h_{dropped}": "-"}) == (dropped,)


def test_a_probe_that_failed_leads_with_why() -> None:
    assert unreadable_facts({**_PRESENT, "h_rc": "7"})[0] == "facts exited 7"
    assert unreadable_facts({"h_error": "archive"})[0].startswith("the target commit's archive")
    assert unreadable_facts({"h_error": "unpack"})[0].startswith("the target commit's archive")
    assert unreadable_facts({"h_error": "boom"})[0] == "probe failed (boom)"


@pytest.mark.parametrize(
    ("facts", "rebuild"),
    [
        ({"h_helper": "present", "h_stale": "no", "h_valid": "yes"}, False),
        ({"h_helper": "present", "h_stale": "yes", "h_valid": "yes"}, True),
        ({"h_helper": "present", "h_stale": "no", "h_valid": "no"}, True),
        ({"h_helper": "absent", "h_stale": "yes", "h_valid": "no"}, False),
        ({}, False),
    ],
)
def test_only_a_helper_that_is_there_can_need_a_rebuild(
    facts: dict[str, str], rebuild: bool
) -> None:
    assert needs_rebuild(facts) is rebuild


def test_a_current_helper_on_every_host_passes() -> None:
    probes = {"mac": _PRESENT, "lin": _PRESENT}
    assert refusals(probes, _TABLE, rebuild=False) == ()


def test_a_certificate_the_fleet_does_not_record_refuses_that_host() -> None:
    probes = {"mac": {**_PRESENT, "h_cert_root": "e" * 40}, "lin": _PRESENT}
    assert refusals(probes, _TABLE, rebuild=False) == (
        f"mac: helper certificate root {'e' * 40} is not the recorded baseline {'f' * 40}",
    )


def test_a_host_without_a_baseline_refuses_rather_than_passes() -> None:
    (reason,) = refusals({"mac": _PRESENT}, {"lin": "f" * 40}, rebuild=False)
    assert reason.startswith("mac: no helper identity baseline recorded")


def test_a_host_with_no_helper_needs_no_baseline() -> None:
    probes = {"gw": {"h_rc": "0", "h_helper": "absent"}}
    assert refusals(probes, {}, rebuild=False) == ()


def test_a_loaded_stale_helper_refuses_unless_the_rebuild_is_accepted() -> None:
    stale = {**_PRESENT, "h_stale": "yes", "h_loaded": "yes"}
    probes = {"mac": stale, "lin": _PRESENT}
    (reason,) = refusals(probes, _TABLE, rebuild=False)
    assert reason.startswith("1 host(s) run a helper this update would have to rebuild")
    assert "mac recorded_hash=" in reason and "lin recorded_hash=" in reason
    assert "add --rebuild-extras" in reason
    assert refusals(probes, _TABLE, rebuild=True) == ()


def test_a_stale_helper_that_is_not_running_is_replaced_in_place_without_a_refusal() -> None:
    probes = {"mac": {**_PRESENT, "h_stale": "yes", "h_loaded": "no"}}
    assert refusals(probes, _TABLE, rebuild=False) == ()


def test_the_hosts_that_cannot_answer_are_named_with_the_fields_they_owe() -> None:
    probes = {"mac": {**_PRESENT, "h_cert_root": "-"}, "lin": {"h_rc": "1"}}
    (reason,) = refusals(probes, _TABLE, rebuild=False)
    assert reason.startswith("could not read the helper facts of the target commit on: ")
    assert "mac owes cert_root" in reason and "lin owes facts exited 1" in reason


def test_a_host_that_could_not_answer_is_not_judged_on_its_baseline() -> None:
    probes = {"mac": {**_PRESENT, "h_cert_root": "-"}}
    assert len(refusals(probes, {}, rebuild=False)) == 1


def test_rebuilding_hosts_are_the_ones_whose_helper_needs_it_and_only_when_accepted() -> None:
    probes = {"mac": {**_PRESENT, "h_stale": "yes"}, "lin": _PRESENT}
    assert rebuilding_hosts(probes, rebuild=True) == frozenset({"mac"})
    assert rebuilding_hosts(probes, rebuild=False) == frozenset()


@pytest.mark.parametrize("field", ["hook", "active"])
@pytest.mark.parametrize("value", ["future", "", "-"])
def test_unknown_host_flags_are_not_negative_answers(field: str, value: str) -> None:
    with pytest.raises(ValueError, match=rf"lin\.{field}.*{value!r}"):
        host_refusals("lin", {**_CLEAN, field: value})


@pytest.mark.parametrize("hook", ["yes", "no"])
@pytest.mark.parametrize("active", ["yes", "no"])
def test_every_valid_host_flag_combination_keeps_its_refusals(hook: str, active: str) -> None:
    reasons = host_refusals("lin", {**_CLEAN, "hook": hook, "active": active})
    assert len(reasons) == (hook == "yes") + (active == "yes")


def test_stop_message_probe_word_is_decoded_explicitly() -> None:
    assert supports_stop_message("lin", {"stop_message": "yes"}) is True
    assert supports_stop_message("lin", {"stop_message": "no"}) is False
    with pytest.raises(ValueError, match="lin: the probe did not report stop_message"):
        supports_stop_message("lin", {})
    with pytest.raises(ValueError, match=r"lin\.stop_message.*'future'"):
        supports_stop_message("lin", {"stop_message": "future"})


@pytest.mark.parametrize("field", ["helper", "valid", "loaded", "stale"])
def test_unknown_helper_words_cannot_bypass_the_gate_or_rebuild_plan(field: str) -> None:
    facts = {**_PRESENT, f"h_{field}": "future"}
    with pytest.raises(ValueError, match=rf"{field}.*'future'"):
        unreadable_facts(facts)
    with pytest.raises(ValueError, match=rf"{field}.*'future'"):
        needs_rebuild(facts)
    with pytest.raises(ValueError, match=rf"mac:.*{field}.*'future'"):
        refusals({"mac": facts}, _TABLE, rebuild=True)
    with pytest.raises(ValueError, match=rf"{field}.*'future'"):
        rebuilding_hosts({"mac": facts}, rebuild=True)


@pytest.mark.parametrize("valid", ["yes", "no"])
@pytest.mark.parametrize("loaded", ["yes", "no"])
@pytest.mark.parametrize("stale", ["yes", "no"])
def test_every_valid_helper_flag_combination_is_judged(valid: str, loaded: str, stale: str) -> None:
    facts = {**_PRESENT, "h_valid": valid, "h_loaded": loaded, "h_stale": stale}
    rebuild = stale == "yes" or valid == "no"
    assert unreadable_facts(facts) == ()
    assert needs_rebuild(facts) is rebuild
    assert bool(refusals({"mac": facts}, _TABLE, rebuild=False)) is (loaded == "yes" and rebuild)
    assert refusals({"mac": facts}, _TABLE, rebuild=True) == ()
