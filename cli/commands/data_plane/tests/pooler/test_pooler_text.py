# meiva:note
# The lines the pooler's bring-up prints: what each failure says and what it tells the operator
# to do next.

from __future__ import annotations

from cli.commands.data_plane.pooler.convergence import BindContext
from cli.commands.data_plane.pooler.text import (
    PoolerText,
    install_command,
    not_installed_error,
    not_installed_warning,
    stale_pidfile,
)

_TEXT = PoolerText(host="10.0.0.5", bind_wait_s=60.0, listen_port=6433, logfile="/h/pgbouncer.log")


def test_a_pooler_born_before_the_address_exists_says_it_would_degrade_silently() -> None:
    line = _TEXT.bind_missing(BindContext.FRESH_START)

    assert "'10.0.0.5' is not assigned to any local interface after 60s" in line
    assert "silently degrade to loopback-only" in line
    assert "private network" in line
    assert "meiva start --all-services" in line


def test_a_degraded_pooler_that_cannot_be_restarted_says_so_and_what_to_retry() -> None:
    line = _TEXT.bind_missing(BindContext.RESTART_OF_DEGRADED)

    assert "'10.0.0.5' is not assigned to any local interface after 60s" in line
    assert "cannot be restarted into a healthy double bind" in line
    assert "meiva start --all-services" in line


def test_a_pooler_that_came_up_on_loopback_only_is_a_loud_failure() -> None:
    line = _TEXT.degraded_after_launch()

    assert "NOT listening on the reachable address '10.0.0.5'" in line
    assert "degraded to loopback-only" in line
    assert "/h/pgbouncer.log" in line
    assert "✓" not in line


def test_the_restart_notes_say_why_the_pooler_restarts() -> None:
    assert "userlist changed" in _TEXT.changed_restart()
    assert "a reload never revokes" in _TEXT.changed_restart()
    assert "NOT listening on the reachable address '10.0.0.5'" in _TEXT.degraded_restart()
    assert "restarting the pooler" in _TEXT.degraded_restart()


def test_a_survivor_of_the_graceful_stop_is_named_and_no_second_pooler_is_promised() -> None:
    line = _TEXT.stop_survived(4242)

    assert "could not stop the running pooler (pid 4242)" in line
    assert "custody retained, not starting a second pooler on the same port" in line


def test_a_failed_launch_carries_the_exit_status_the_log_and_the_stderr() -> None:
    line = _TEXT.launch_failed(1, "  could not bind\n")

    assert line == ("  ✗ pgbouncer start failed (rc=1); see /h/pgbouncer.log\n    could not bind")


def test_the_success_lines_name_the_loopback_port() -> None:
    assert _TEXT.started() == "  ✓ pgbouncer started (127.0.0.1:6433, transaction pooling)"
    assert _TEXT.reloaded() == "  ✓ pgbouncer already running (127.0.0.1:6433), reloaded"
    assert "6433" in _TEXT.not_ready() and "/h/pgbouncer.log" in _TEXT.not_ready()
    assert "refused its admin credential" in _TEXT.admin_refused()


def test_the_install_hint_is_brew_on_macos_and_the_pinned_provisioner_elsewhere() -> None:
    assert (
        install_command(macos=True, provision_script="/r/database.sh") == "brew install pgbouncer"
    )
    assert (
        install_command(macos=False, provision_script="/r/database.sh")
        == "sudo bash /r/database.sh"
    )


def test_a_pooler_that_is_not_installed_fails_bring_up_and_only_warns_converge() -> None:
    error = not_installed_error("/x/pgbouncer", "brew install pgbouncer")
    warning = not_installed_warning("/x/pgbouncer", "brew install pgbouncer")

    assert "MEIVA_PGBOUNCER_ENABLED is set but pgbouncer is not installed ('/x/pgbouncer')" in error
    assert "unset MEIVA_PGBOUNCER_ENABLED to run direct" in error
    assert (
        warning
        == "  ! pgbouncer enabled but not installed ('/x/pgbouncer') — run `brew install pgbouncer`"
    )


def test_a_stale_pidfile_names_the_stranger_and_that_it_is_not_signalled() -> None:
    line = stale_pidfile("/h/pgbouncer.pid", 77)

    assert "/h/pgbouncer.pid names pid 77, which is not this home's pooler" in line
    assert "not signalling it" in line
