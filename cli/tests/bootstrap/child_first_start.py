# meiva:note
# A fresh `meiva init` and the sinks of the `meiva start` that follows it, as a real process does them.
#
# The start's lifecycle half is not run: a loguru warning is provoked inside the admitted
# start instead (a pgvector pre-create against a closed port), which only the sinks the
# start opened can keep.

import os
from pathlib import Path

from base import telemetry
from base.cluster import ensure_pgvector_extension
from base.log import prepare_process_logging
from cli import init_intent
from cli.database import open_operator_database, operator_database_factory, operator_event_pipeline
from cli.database.runtime import CommandRuntime
from cli.parsers import build_parser
from cli.start_intent import admitted_home
from cli.start_runtime import HomeSite
from cli.startup.log_sinks import START_SINK
from cli.startup.service_path import CallerHost

prepare_process_logging()
home = Path(os.environ["MEIVA_HOME"])
checkout = home.parent / "checkout"
checkout.mkdir()
site = HomeSite(home, checkout)
init_args = build_parser().parse_args(
    ["init", "--serve-gateway", "--serve-agent-runner", "--machine-name", "probe"]
)
caller = CallerHost(
    service_path=None,
    path="/usr/bin:/bin",
    virtual_env=None,
    prefix_venv=None,
    occupied_ports=frozenset(),
)
assert init_intent.run_init(init_args, site, caller) == 0

command = CommandRuntime.create(home.resolve())
database_factory = operator_database_factory(
    local_host=command.machine.host, open_database=open_operator_database
)
producer = operator_event_pipeline(database_factory, machine=command.machine, home=home)
try:
    with admitted_home(home):
        producer.initialize_logging(START_SINK)
        ensure_pgvector_extension(
            "meiva", base_admin_url="postgresql://probe@127.0.0.1:1/postgres?connect_timeout=5"
        )
finally:
    telemetry.sync(timeout=5.0)
    producer.close_command(primary=None)
