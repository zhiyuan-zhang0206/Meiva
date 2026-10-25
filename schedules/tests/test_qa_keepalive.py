# meiva:note
# The QA keepalive's two decisions: when a streak is worth a wake (`plan_round`) and what a site
# must supply before the keeper will watch anything (`Settings.from_environ`).
#
# The probes themselves are deliberately absent here -- they are two remote reads whose failure
# modes are `unknown` versus `down`, and the rule is what turns those into a wake.

from __future__ import annotations

import pytest

from schedules.jobs.qa_keepalive import (
    ENV_AGENT,
    ENV_HOST,
    ENV_PROCESS,
    ENV_SSH_TARGETS,
    ENV_URL,
    Settings,
)
from schedules.plans.keepalive_rule import (
    BLIND_AFTER_ROUNDS,
    COOLDOWN_SECONDS,
    KeepaliveState,
    RoundContext,
    plan_round,
)

_CONTEXT = RoundContext(
    stamp="2026-10-25T06:00:00+00:00",
    host="company-mini",
    hint="restart the page",
    blind_hint="check the network path",
)
_DOWN = ("page health check http://example/health is not 200",)


def _plan(state: KeepaliveState, problems: tuple[str, ...], *, blind: bool, now: float):
    return plan_round(state, problems, blind=blind, now=now, context=_CONTEXT)


def test_one_failed_round_does_not_wake() -> None:
    plan = _plan(KeepaliveState(), _DOWN, blind=False, now=1000.0)
    assert plan.wake is None
    assert plan.state.fails == 1
    assert plan.state.first_fail_at == _CONTEXT.stamp


def test_consecutive_failures_wake_once_and_hold_the_cooldown() -> None:
    first = _plan(KeepaliveState(), _DOWN, blind=False, now=1000.0)
    second = _plan(first.state, _DOWN, blind=False, now=1600.0)
    assert second.wake is not None
    assert "2 consecutive failed rounds" in second.wake
    assert "restart the page" in second.wake

    during = _plan(second.state, _DOWN, blind=False, now=1600.0 + COOLDOWN_SECONDS - 1)
    assert during.wake is None

    after = _plan(during.state, _DOWN, blind=False, now=1600.0 + COOLDOWN_SECONDS + 1)
    assert after.wake is not None


def test_a_healthy_round_clears_both_streaks() -> None:
    down = _plan(KeepaliveState(), _DOWN, blind=False, now=1000.0)
    blind = _plan(down.state, (), blind=True, now=1100.0)
    healthy = _plan(blind.state, (), blind=False, now=1200.0)
    assert healthy.state.fails == 0
    assert healthy.state.first_fail_at is None
    assert healthy.state.blind == 0
    assert healthy.state.blind_since is None
    assert healthy.wake is None


def test_blind_rounds_wake_separately_from_an_outage() -> None:
    state = KeepaliveState()
    for round_number in range(1, BLIND_AFTER_ROUNDS):
        state = _plan(state, (), blind=True, now=1000.0 + round_number).state
        assert state.fails == 0

    woke = _plan(state, (), blind=True, now=2000.0)
    assert woke.wake is not None
    assert "could not probe" in woke.wake
    assert "check the network path" in woke.wake
    assert woke.state.last_blind_wake_at == 2000.0


def test_only_a_determinate_answer_clears_the_blind_streak() -> None:
    state = KeepaliveState()
    for round_number in range(BLIND_AFTER_ROUNDS):
        state = _plan(state, (), blind=True, now=1000.0 + round_number).state
    assert state.blind == BLIND_AFTER_ROUNDS

    still_blind = _plan(state, (), blind=True, now=1500.0)
    assert still_blind.state.blind == BLIND_AFTER_ROUNDS + 1

    cleared = _plan(still_blind.state, (), blind=False, now=1600.0)
    assert cleared.state.blind == 0
    assert cleared.state.blind_since is None


def _env(**overrides: str) -> dict[str, str]:
    base = {
        ENV_URL: "http://example/health",
        ENV_AGENT: "5772",
        ENV_HOST: "company-mini",
    }
    return {**base, **overrides}


def test_a_site_without_a_url_is_told_which_variable_is_missing() -> None:
    with pytest.raises(RuntimeError, match=ENV_URL):
        Settings.from_environ({ENV_AGENT: "5772", ENV_HOST: "company-mini"})


def test_a_process_probe_needs_a_host_to_probe() -> None:
    with pytest.raises(RuntimeError, match=ENV_SSH_TARGETS):
        Settings.from_environ(_env(**{ENV_PROCESS: "watcher.py"}))


def test_a_page_only_site_needs_no_ssh_targets() -> None:
    settings = Settings.from_environ(_env())
    assert settings.process_pattern == ""
    assert settings.ssh_targets == ()
    assert settings.report_agent == 5772


def test_ssh_targets_are_split_and_trimmed() -> None:
    settings = Settings.from_environ(
        _env(**{ENV_PROCESS: "watcher.py", ENV_SSH_TARGETS: " company-mini , second@10.0.0.5 "})
    )
    assert settings.ssh_targets == ("company-mini", "second@10.0.0.5")


def test_a_non_numeric_owner_is_refused() -> None:
    with pytest.raises(RuntimeError, match=ENV_AGENT):
        Settings.from_environ(_env(**{ENV_AGENT: "me"}))
