# meiva:note
# What the window concludes from NEW's answer about the root a host keeps: when the root is kept at
# all, the command each host runs, and whether a changed generation refuses, a check that could
# not run refuses, or the host passes.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.md
from __future__ import annotations

import pytest

from cli.fleet_update.plan.root_generation import root_check_script, root_is_kept, root_refusal

NEW = "b" * 40


@pytest.mark.parametrize(
    ("shell", "kept", "expected"),
    [
        ("--keep-shell-sessions", "--keep-no-services", True),
        ("--close-shell-sessions", "--keep-service", True),
        ("--close-shell-sessions", "--keep-no-services", False),
    ],
)
def test_the_root_is_kept_while_any_unit_is(shell: str, kept: str, expected: bool) -> None:
    assert root_is_kept(shell=shell, kept=kept) is expected


def test_the_command_names_new_the_hosts_checkout_and_the_quoted_selection() -> None:
    template = '{home}\nW=x\n{new}\npython -c {program} "$S" {selection}\n'

    script = root_check_script(template, new=NEW, selection="--disable-service", names=("a b", "c"))

    assert NEW in script
    assert "live_generation import main" in script
    assert script.endswith("\"$S\" --disable-service 'a b' c\n")


@pytest.mark.parametrize("verdict", ["same", "none"])
def test_a_root_that_serves_new_or_no_root_passes(verdict: str) -> None:
    refusal = root_refusal("gw", f"ROOT_GENERATION={verdict}\nROOT_CHECK_RC=0", new=NEW)

    assert refusal is None


def test_a_changed_generation_refuses_and_says_what_to_choose() -> None:
    output = (
        "ROOT_GENERATION=changed: requested services change the immutable root generation: "
        "root launch inputs; run meiva stop before starting with changed services or "
        "configuration\nROOT_CHECK_RC=0"
    )

    refusal = root_refusal("gw", output, new=NEW)

    assert refusal is not None
    assert refusal.startswith("gw: the meiva-root this window keeps cannot serve bbbbbbbbbbbb")
    assert "root launch inputs" in refusal
    assert "--close-shell-sessions with --keep-no-services" in refusal


def test_a_check_that_could_not_run_refuses() -> None:
    refusal = root_refusal("lin", "Traceback (most recent call last)\nROOT_CHECK_RC=1", new=NEW)

    assert refusal is not None and "check exited 1" in refusal


def test_a_live_root_is_not_absent_when_an_old_envelope_cannot_be_read() -> None:
    refusal = root_refusal("gw", "ROOT_GENERATION=none\nROOT_CHECK_RC=0", new=NEW, was_running=True)

    assert refusal is not None and "cannot prove the kept meiva-root" in refusal
