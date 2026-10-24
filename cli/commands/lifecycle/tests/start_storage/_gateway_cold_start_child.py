# meiva:note
# A gateway's cold-start preparation in a fresh interpreter, composed as the entry composes it.
#
# The data-plane birth adopts the delivered gateway login into the process environment, which a
# test process must not carry away, so the whole preparation runs here and prints what it did as
# one JSON line. Run as `python -I -B _gateway_cold_start_child.py <repo>`; the home and the data
# plane are the unit environment the parent delivers, read the way the start reads them.

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from base.deploy import start_trace
from base.host.db_authority import delivery
from cli.commands.converge.spec import ConvergeStep
from cli.commands.converge.steps import ensure_meiva_home_dirs
from cli.commands.data_plane.config import data_plane_config_from_boot
from cli.commands.lifecycle.start.cold_start import ColdStart, prepare_cold_start
from cli.database import (
    open_operator_database,
    operator_database_factory,
    operator_event_pipeline,
)
from cli.database.runtime import CommandRuntime


def main() -> None:
    repo = Path(sys.argv[1])
    command = CommandRuntime.create()
    database_factory = operator_database_factory(
        local_host=command.machine.host, open_database=open_operator_database
    )
    producer = operator_event_pipeline(database_factory, machine=command.machine, home=command.home)
    try:
        cold = ColdStart(
            command=command,
            database_factory=database_factory,
            producer=producer,
            repo=repo,
            roles=frozenset({"gateway"}),
            plane=data_plane_config_from_boot(command.config),
            converge_steps=(ConvergeStep("$MEIVA_HOME dir skeleton", ensure_meiva_home_dirs),),
        )
        children: list[subprocess.Popen[bytes]] = []
        rc = prepare_cold_start(cold, (), retained_children=children)
    finally:
        producer.close()
    spans = sorted(start_trace.trace().spans, key=lambda span: span[1])
    print(
        json.dumps(
            {
                "rc": rc,
                "phases": [name for name, _start, _took in spans],
                "adopted_url": database_factory.db_url(),
                "generation": os.environ.get(delivery.GENERATION_ENV),
            }
        )
    )


main()
