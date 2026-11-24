import random
from pathlib import Path
from typing import Protocol, cast

import pytest

from base import telemetry
from base.clock.tests.fakes import system_clock
from base.telemetry.tests.otlp.identity_inputs import machine_identity


class _HomedExporter(Protocol):
    _home: Path


# meiva:note
# The exporter's resource identity derives from the home, so a relative home would name the working
# directory: the boundary refuses it before anything is built.
def test_a_relative_home_is_refused_before_any_sink_is_built() -> None:
    with pytest.raises(ValueError, match="absolute home"):
        telemetry.build_pipeline(
            now=system_clock().now,
            machine=machine_identity(),
            home=Path(".meiva"),
            otlp_enabled=lambda: False,
            otlp_endpoint=lambda: "http://127.0.0.1:4318",
            rng=random.SystemRandom(),
        )


# meiva:note
# The pipeline's own exporter is built over the home the pipeline was given, not the process's:
# the home here is a directory under `tmp_path`.
def test_the_pipeline_builds_its_exporter_over_its_own_home(tmp_path: Path) -> None:
    home = tmp_path / "pipeline-home"
    home.mkdir()
    pipeline = telemetry.build_pipeline(
        now=system_clock().now,
        machine=machine_identity(),
        home=home,
        otlp_enabled=lambda: False,
        otlp_endpoint=lambda: "http://127.0.0.1:4318",
        rng=random.SystemRandom(),
    )
    try:
        exporter = cast(
            "_HomedExporter",
            pipeline._exporter,  # pyright: ignore[reportAttributeAccessIssue]
        )
        assert exporter._home == home
    finally:
        pipeline.stop(timeout=2)
