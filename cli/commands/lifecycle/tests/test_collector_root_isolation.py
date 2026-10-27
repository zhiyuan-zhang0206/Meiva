# meiva:note
# Preparing the collector sidecar never reaches the running root.
#
# The converge step writes the collector's binary marker and config under the home and nothing else:
# the live service tree it runs next to is restarted by `meiva-root`, never by a converge pass. The
# root here is a real listener on a unix socket under the test's home, so a request to it would be
# recorded; the step runs for real over a station home whose pinned binary is already installed.

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

from base.deploy.release import collector_artifact
from base.host.env.dotenv_boot import PLACEHOLDER_DB_URL
from base.host.tests.short_tmp import short_tmp as short_tmp
from base.paths import otel_collector_config, otel_collector_dir
from base.telemetry import EventPipeline
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.lifecycle.tests.scripted_root import UnitRow, scripted_root
from cli.commands.observability import otel_collector as oc
from cli.database.runtime import CommandRuntime, UserSession
from cli.database.tests.command_inputs import command_runtime as command_runtime


def _never_dialed() -> NoReturn:
    raise AssertionError("the collector step of a gateway must not open the database")


def _never_produced() -> EventPipeline:
    raise AssertionError("the collector step must not emit events")


def test_collector_preparation_never_controls_a_running_service_tree(
    short_tmp: Path, command_runtime: CommandRuntime
) -> None:
    home = short_tmp
    (home / "lgtm-host").write_text("", encoding="utf-8")
    command_runtime.machine.set(name="test-machine", role=frozenset({"gateway"}))
    command_runtime.config.set_field("db_url", PLACEHOLDER_DB_URL)
    installed = otel_collector_dir(home)
    installed.mkdir(parents=True)
    (installed / collector_artifact.BINARY_NAME).write_bytes(b"bin")
    (installed / collector_artifact.VERSION_MARKER).write_text(
        collector_artifact.OTELCOL_CONTRIB_VERSION, encoding="utf-8"
    )
    ctx = ConvergeCtx(
        repo=Path(__file__).resolve().parents[4],
        meiva_home=home,
        roles=frozenset({"gateway"}),
        config=command_runtime.config,
        machine=command_runtime.machine,
        database_factory=_never_dialed,
        producer=_never_produced,
        helper=command_runtime.helper,
        host=command_runtime.host,
        session=UserSession(user_home=home, login_shell="/bin/zsh", interactive=False),
        timers=command_runtime.timers,
        clock=command_runtime.clock,
        services=frozenset(),
    )

    with scripted_root(home, [UnitRow("gateway")]) as root:
        oc.ensure_otel_collector_step(ctx)

    # meiva:note
    # The step did prepare the collector (its config is on disk), and no request reached the live
    # root while it did.
    assert otel_collector_config(home).is_file()
    assert root.calls == []
