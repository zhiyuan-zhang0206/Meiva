# meiva:note
# The words each host is sent, and what the window keeps out of its own log. A command is a value:
# these pin the shape every host receives -- the gateway's flags, the checkout order, the
# one-shot job on macOS -- and that a credential never reaches the console or the log.
# meiva:link cli/docs/fleet-update.meiva.okf.md
from __future__ import annotations

import shlex

import pytest

from cli.fleet_update.oneshot import start_script
from cli.fleet_update.plan.gateway_commands import (
    drift_command,
    machine_name_command,
    pre_verify_script,
    refresh_command,
    roster_command,
    smoke_command,
)
from cli.fleet_update.plan.host_commands import (
    CLEAN,
    HOME,
    fetch_command,
    helper_probe_command,
    login_shell,
    start_command,
    stop_command,
    switch_command,
)
from cli.fleet_update.plan.redaction import redact

_NEW = "0123456789abcdef0123456789abcdef01234567"


def test_a_stop_always_confirms_and_carries_the_flags_it_was_given() -> None:
    assert stop_command("--keep-browser --mode smooth --timeout 60") == (
        f'{HOME}; MEIVA_HOME="$H" "$S/.venv/bin/meiva" stop --yes '
        "--keep-browser --mode smooth --timeout 60"
    )


def test_the_switch_checks_new_out_detached_syncs_the_venv_and_requires_a_clean_tree() -> None:
    command = switch_command(_NEW)
    steps = command.split(" && ")
    assert steps[0].endswith(f"git checkout -q --detach {_NEW}")
    assert steps[1].startswith("find .venv/bin .venv/lib") and "chmod u+w" in steps[1]
    assert steps[2] == "env -u VIRTUAL_ENV uv sync --frozen --compile-bytecode"
    assert steps[3] == CLEAN


def test_the_fetch_is_skipped_when_the_commit_is_already_there() -> None:
    assert fetch_command(_NEW).endswith(
        f'git cat-file -e "{_NEW}^{{commit}}" || git fetch -q origin {_NEW}'
    )


def test_a_start_carries_the_operators_selection() -> None:
    assert start_command("--only-service gateway").endswith(
        'MEIVA_HOME="$H" "$S/.venv/bin/meiva" start --only-service gateway'
    )


@pytest.mark.parametrize(
    ("os_name", "shell"), [("Darwin", "zsh"), ("Linux", "bash"), ("FreeBSD", "bash")]
)
def test_a_host_runs_its_command_in_its_own_login_shell(os_name: str, shell: str) -> None:
    command = login_shell(os_name, "echo 'a b'")
    assert shlex.split(command) == [shell, "-lc", "echo 'a b'"]


def test_the_helper_probe_reads_the_target_commits_objects_not_the_installed_checkout() -> None:
    command = helper_probe_command(_NEW)
    assert f'git -C "$S" archive {_NEW} | tar -x -C "$tmp"' in command
    assert (
        'PYTHONPATH="$tmp"' in command
        and '"$tmp/scripts/host_ops/tcc/helper-signing.py"' in command
    )
    assert '"$S/scripts/host_ops/tcc/helper-signing.py"' not in command


def test_the_gateway_programs_are_asked_by_verb() -> None:
    assert roster_command().endswith('"$S/.venv/bin/python" - roster')
    assert smoke_command("mac mini", 300).endswith(" - smoke 'mac mini' 300")
    assert refresh_command().endswith('meiva" packages refresh')
    assert drift_command("plugins verify").endswith('meiva" plugins verify')


def test_the_machine_name_is_what_the_host_itself_boots_into() -> None:
    command = machine_name_command()
    assert "machine_from_boot(ConfigBoot()).name()" in command
    assert command.count("python") == 1 and command.endswith('.name())"')


def test_the_pre_update_check_quotes_both_programs_into_the_gateways_script() -> None:
    template = "{home}\nread: {read_rows}\ncheck: {check_rows}\nnew: {new}"
    program = "print('a b')\n"
    script = pre_verify_script(template, new=_NEW, read_rows=program, check_rows="x")
    assert script == f"{HOME}\nread: {shlex.quote(program)}\ncheck: x\nnew: {_NEW}"


def test_a_linux_host_starts_over_ssh_and_a_macos_host_through_a_one_shot_job() -> None:
    plain = start_script(macos=False, alias="lin", stamp="S", timeout_s=60, flags="--all-services")
    assert plain == start_command("--all-services")
    job = start_script(macos=True, alias="mac", stamp="S", timeout_s=60, flags="--all-services")
    assert 'launchctl bootstrap "gui/$(id -u)"' in job and "ONESHOT_RC" in job
    assert "com.meiva.fleet-update.mac.S" in job


def test_the_one_shot_waits_a_fifth_of_the_start_timeout_in_five_second_polls() -> None:
    job = start_script(macos=True, alias="mac", stamp="S", timeout_s=1800, flags="--all-services")
    assert '[ "$n" -ge 360 ]' in job and "sleep 5" in job


def test_an_alias_is_made_safe_for_the_job_label() -> None:
    job = start_script(macos=True, alias="mac mini/1", stamp="S", timeout_s=60, flags="x")
    assert "com.meiva.fleet-update.mac-mini-1.S" in job


@pytest.mark.parametrize(
    ("line", "kept", "gone"),
    [
        ("MEIVA_CLUSTER_SECRET=s3cret-a", "MEIVA_CLUSTER_SECRET=<redacted>", "s3cret-a"),
        ("Authorization: Bearer tok-b", "Bearer <redacted>", "tok-b"),
        ("postgresql://u:pw-c@h/db", "postgresql://u:<redacted>@h/db", "pw-c"),
        ('{"api_key": "k-d"}', '"api_key": "<redacted>"', "k-d"),
        ("capability_key: ck-e, next", "capability_key: <redacted>, next", "ck-e"),
        ("db_password='p-f'", "db_password='<redacted>'", "p-f"),
    ],
)
def test_a_credential_in_a_line_is_replaced_and_the_rest_of_the_line_is_kept(
    line: str, kept: str, gone: str
) -> None:
    out = redact(line)
    assert kept in out and gone not in out


def test_a_line_without_a_credential_is_left_alone() -> None:
    line = 'head=0123 op={"operation": "stop"} https://example.invalid/path'
    assert redact(line) == line
