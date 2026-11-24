import sys
from datetime import UTC, datetime

from base.clock.tests.fakes import system_clock
from base.paths import meiva_home
from cli.commands.management.schedules_verify import RowsFromFile, cmd_schedules_verify
from cli.database import operator_event_pipeline
from cli.database.runtime import CommandRuntime


def _check_rows(rows_file: str) -> None:
    command = CommandRuntime.create(meiva_home().resolve())
    producer = operator_event_pipeline(
        machine=command.machine,
        home=meiva_home(),
        now=system_clock().now,
        otlp_enabled=command.otlp_enabled,
        otlp_endpoint=command.otlp_endpoint,
        rng=command.rng,
    )
    try:
        sys.exit(
            cmd_schedules_verify(
                python=sys.executable,
                source=RowsFromFile(rows_file),
                notify=False,
                now=datetime.now(UTC),
                producer=producer,
                children=1,
            )
        )
    finally:
        producer.close()


if __name__ == "__main__":
    if sys.argv[1] == "check_rows":
        _check_rows(sys.argv[2])
    else:
        raise SystemExit(f"unknown scenario {sys.argv[1]!r}")
