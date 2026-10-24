# meiva:note
# `meiva cluster destroy` end to end: a confirmed decommission of a real home.
#
# The home is born the way `meiva init` leaves it (a start intent on disk under `tmp_path`), the
# checkout holds a `.venv/bin/meiva` that is a real script the destroy runs as a child, and the
# person at the terminal is a pair of streams that answer `isatty()` and `readline()`. What the
# destroy asks, says and does is decided from values in `test_destroy_plan.py`; this file proves
# the shell executes that plan in order against the real home and stops where it must: an
# unconfirmed destroy changes nothing, and a failure leaves the home attached with its destroy
# intent retained.

from __future__ import annotations

import io
from pathlib import Path
from typing import Protocol

import pytest

from base.cluster.dataplane.pg_admin import pg_socket_path
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import FakeCommands, Reply, fake_host
from base.paths.tests.homes import default_home as default_home
from base.paths.tests.homes import unit_home as unit_home
from cli import start_identity
from cli.commands.cluster.home import (
    DestroyRequest,
    Terminal,
    cmd_cluster_destroy,
    unregister_scheduled_jobs,
)
from cli.commands.cluster.plans.destroy_plan import STOP_INCOMPLETE
from cli.commands.cluster.plans.destroy_plan import stop_argv as stop_argv


# meiva:note
# A stream that says whether it is a terminal, as `sys.stdin` and `sys.stdout` do.
class _Stream(io.StringIO):
    def __init__(self, initial: str = "", *, tty: bool = True) -> None:
        super().__init__(initial)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


# meiva:note
# One person at a terminal: what they type and what each stream received.
class Session:
    def __init__(self, typed: str, *, stdin_tty: bool = True, stdout_tty: bool = True) -> None:
        self.stdin = _Stream(typed, tty=stdin_tty)
        self.stdout = _Stream(tty=stdout_tty)
        self.stderr = _Stream(tty=False)

    @property
    def terminal(self) -> Terminal:
        return Terminal(self.stdin, self.stdout, self.stderr)


def _anything(_argv: tuple[str, ...]) -> bool:
    return True


def _detached(home: Path) -> bool:
    marker = home / "destroy-intent.json"
    return marker.exists() and '"detached"' in marker.read_text()


def _destroying(home: Path) -> bool:
    marker = home / "destroy-intent.json"
    return marker.exists() and '"destroying"' in marker.read_text()


@pytest.fixture
def roles() -> list[str]:
    return ["agent-runner"]


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A checkout whose `meiva` records where it ran and with what, then exits as scripted."""
    root = tmp_path / "checkout"
    script = root / ".venv" / "bin" / "meiva"
    script.parent.mkdir(parents=True)
    script.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$PWD" "$@" > "$0.ran"\nexit "$(cat "$0.rc" 2>/dev/null || echo 0)"\n'
    )
    script.chmod(0o755)
    return root


def _stop_ran(checkout: Path) -> list[str] | None:
    ran = checkout / ".venv" / "bin" / "meiva.ran"
    return ran.read_text().splitlines() if ran.exists() else None


def _stop_exits_with(checkout: Path, code: int) -> None:
    (checkout / ".venv" / "bin" / "meiva.rc").write_text(str(code))


# meiva:note
# `home` as `meiva init` leaves it: its start intent records the capabilities `roles`.
@pytest.fixture
def home(tmp_path: Path, checkout: Path, roles: list[str]) -> Path:
    home = (tmp_path / "home").resolve()
    home.mkdir(mode=0o700)
    start_identity.prepare_identity(
        start_identity.IdentityInput(
            home,
            checkout,
            frozenset(roles),
            {"MEIVA_MACHINE_NAME": "destroyed", "MEIVA_GATEWAY_URL": "http://10.0.0.7:8000"},
            occupied_ports=frozenset(),
        )
    )
    return home


def _request(home: Path, checkout: Path, *, drop_db: bool = False) -> DestroyRequest:
    return DestroyRequest(
        home=home,
        drop_db=drop_db,
        stop_mode="smooth",
        stop_timeout_s=300.0,
        checkout=checkout,
        helper_port=8181,
    )


# meiva:note
# `meiva cluster destroy` against the test's home, run for one session.
class Destroy(Protocol):
    def __call__(
        self, session: Session, *, drop_db: bool = False, host: Host | None = None
    ) -> int: ...


@pytest.fixture
def destroy(home: Path, checkout: Path) -> Destroy:
    def run(session: Session, *, drop_db: bool = False, host: Host | None = None) -> int:
        return cmd_cluster_destroy(
            _request(home, checkout, drop_db=drop_db),
            host=fake_host() if host is None else host,
            terminal=session.terminal,
        )

    return run


def test_a_confirmed_destroy_stops_the_cluster_from_its_checkout_and_marks_the_home_detached(
    home: Path, checkout: Path, destroy: Destroy
) -> None:
    session = Session(f"{home}\n")

    assert destroy(session) == 0

    assert _detached(home)
    ran = _stop_ran(checkout)
    assert ran == [
        str(checkout.resolve()),
        *stop_argv("meiva", ["agent-runner"], "smooth", 300.0)[1:],
    ]
    out = session.stdout.getvalue()
    assert f"will decommission the cluster at {home} (agent-runner)" in out
    assert out.endswith(f"Type the home path to confirm: detached {home} after exact cleanup\n")
    assert session.stderr.getvalue() == ""


# meiva:note
# Both streams must be terminals: there is no flag that skips the prompt.
@pytest.mark.parametrize(
    ("stdin_tty", "stdout_tty"), [(False, False), (True, False), (False, True)]
)
def test_destroy_refuses_without_an_interactive_terminal(
    home: Path, checkout: Path, destroy: Destroy, stdin_tty: bool, stdout_tty: bool
) -> None:
    session = Session(f"{home}\n", stdin_tty=stdin_tty, stdout_tty=stdout_tty)

    assert destroy(session) == 1

    assert "interactive terminal" in session.stderr.getvalue()
    assert session.stdout.getvalue() == ""
    assert _stop_ran(checkout) is None
    assert not (home / "destroy-intent.json").exists()


@pytest.mark.parametrize("answer", ["", "y", "yes", "/somewhere/else", "home"])
def test_a_different_answer_changes_nothing(
    home: Path, checkout: Path, destroy: Destroy, answer: str
) -> None:
    data = home / "pg"
    data.mkdir()
    session = Session(f"{answer}\n")

    assert destroy(session, drop_db=True) == 1

    assert session.stderr.getvalue() == "aborted: nothing was changed\n"
    assert _stop_ran(checkout) is None
    assert data.is_dir()
    assert not (home / "destroy-intent.json").exists()


def test_end_of_input_changes_nothing(home: Path, checkout: Path, destroy: Destroy) -> None:
    session = Session("")

    assert destroy(session) == 1

    assert "aborted: nothing was changed" in session.stderr.getvalue()
    assert _stop_ran(checkout) is None
    assert not (home / "destroy-intent.json").exists()


@pytest.mark.parametrize("roles", [["agent-runner", "gateway"]])
def test_the_prompt_says_what_happens_and_what_is_deleted(home: Path, destroy: Destroy) -> None:
    session = Session(f"{home}\n")

    assert destroy(session, drop_db=True) == 0

    out = session.stdout.getvalue()
    assert "(agent-runner, gateway)" in out
    assert "serves the gateway" in out
    assert "cannot be undone" in out
    for directory in (home / "pg", home / "redis", pg_socket_path(home)):
        assert str(directory) in out


def test_the_prompt_lists_no_deletion_without_drop_db(home: Path, destroy: Destroy) -> None:
    session = Session(f"{home}\n")

    assert destroy(session) == 0

    out = session.stdout.getvalue()
    assert "DELETE" not in out and "cannot be undone" not in out
    assert "serves the gateway" not in out


def test_a_home_without_a_start_intent_is_refused_before_any_prompt(
    home: Path, checkout: Path, destroy: Destroy
) -> None:
    (home / start_identity.INTENT_NAME).unlink()
    session = Session(f"{home}\n")

    assert destroy(session) == 1

    assert session.stderr.getvalue() == f"✗ meiva cluster destroy: no start intent at '{home}'\n"
    assert session.stdout.getvalue() == ""
    assert _stop_ran(checkout) is None
    assert not (home / "destroy-intent.json").exists()


def test_a_failed_stop_leaves_the_home_attached_with_the_destroy_intent_retained(
    home: Path, checkout: Path, destroy: Destroy
) -> None:
    _stop_exits_with(checkout, 4)
    session = Session(f"{home}\n")

    assert destroy(session) == 4

    assert session.stderr.getvalue() == f"{STOP_INCOMPLETE}\n"
    assert _destroying(home) and not _detached(home)


def test_destroy_preserves_credentials_and_data_without_drop_db(
    home: Path, destroy: Destroy
) -> None:
    env = home / ".env"
    env.write_text("PRIVATE_KEY=retain\n")
    data = home / "pg" / "data"
    data.mkdir(parents=True)

    assert destroy(Session(f"{home}\n")) == 0

    assert env.read_text() == "PRIVATE_KEY=retain\n"
    assert data.is_dir()


def test_drop_db_removes_the_data_directories_after_the_stop_and_keeps_the_env(
    home: Path, checkout: Path, destroy: Destroy
) -> None:
    (home / "pg" / "data").mkdir(parents=True)
    (home / "redis").mkdir()
    (home / ".env").write_text("KEEP=1\n")
    session = Session(f"{home}\n")

    assert destroy(session, drop_db=True) == 0

    assert _stop_ran(checkout) is not None
    assert not (home / "pg").exists() and not (home / "redis").exists()
    assert (home / ".env").read_text() == "KEEP=1\n"
    assert session.stdout.getvalue().endswith(
        f"✓ removed '{home}' per-cluster data plane (pg + redis data dirs)\n"
    )


# meiva:note
# Labels name a job, not a home: only the default home may remove one.
def test_a_scratch_home_never_reaches_the_hosts_scheduler(
    unit_home: Path, home: Path, destroy: Destroy
) -> None:
    commands = FakeCommands()

    assert destroy(Session(f"{home}\n"), host=fake_host(commands)) == 0

    assert commands.calls == []


# meiva:note
# A cleanup that could not finish leaves the home attached with its destroy intent retained;
# running destroy again completes it. The default home is the only one that owns OS jobs, so
# this is the home whose scheduler the host is asked to clear.
def test_a_failure_to_retire_the_jobs_leaves_the_home_attached_and_a_rerun_completes_it(
    default_home: Path, tmp_path: Path, checkout: Path
) -> None:
    prod = (default_home / ".meiva").resolve()
    prod.mkdir(mode=0o700)
    start_identity.prepare_identity(
        start_identity.IdentityInput(
            prod,
            checkout,
            frozenset({"gateway"}),
            {"MEIVA_MACHINE_NAME": "destroyed", "MEIVA_GATEWAY_URL": "http://10.0.0.7:8000"},
            occupied_ports=frozenset(),
        )
    )
    broken = FakeCommands()
    broken.on(_anything, OSError("scheduler unavailable"), repeat=True)
    working = FakeCommands()
    working.on(_anything, Reply(), repeat=True)

    first = Session(f"{prod}\n")
    failed = cmd_cluster_destroy(
        _request(prod, checkout), host=fake_host(broken), terminal=first.terminal
    )
    assert failed == 1
    assert first.stderr.getvalue().startswith("cluster cleanup incomplete: could not remove")
    assert _destroying(prod) and not _detached(prod)

    second = Session(f"{prod}\n")
    done = cmd_cluster_destroy(
        _request(prod, checkout), host=fake_host(working), terminal=second.terminal
    )
    assert done == 0 and _detached(prod)
    assert working.calls


def test_every_scheduled_job_is_tried_even_when_the_first_fails(default_home: Path) -> None:
    broken = FakeCommands()
    broken.on(_anything, OSError("launchctl unavailable"), repeat=True)

    with pytest.raises(RuntimeError) as failed:
        unregister_scheduled_jobs(host=fake_host(broken))

    message = str(failed.value)
    assert message.startswith("could not remove scheduled jobs: health probe (")
    for job in ("logs maintenance", "packages refresh", "PR flow", "WAL-G tick"):
        assert f"{job} (launchctl unavailable)" in message


def test_the_crontab_is_read_once_per_cron_based_job_on_a_default_home(
    default_home: Path,
) -> None:
    # meiva:note
    # On Linux without systemd the health probe, logs maintenance, packages refresh, PR flow and
    # WAL-G tick are crontab entries; the boot job has nothing to ask the host.
    working = FakeCommands()
    working.on(_anything, Reply(), repeat=True)

    unregister_scheduled_jobs(host=fake_host(working))

    assert working.argvs == [("crontab", "-l")] * 5
