# meiva:note
# The converge shell's download of the pinned collector against a real release mirror on port 0.
#
# The unit's opener is the system opener pointed at the mirror, so the request, the retry and the
# digest check run over a real socket and real files. The pin is fixed by the release owner, so a
# mirror cannot serve a tarball that passes it: these cases prove the shell fetches through the
# opener it was given, retries a flaky mirror, and installs nothing from bytes that do not match.
# meiva:link cli/commands/observability/docs/operations.md

from __future__ import annotations

from dataclasses import replace

import pytest

from base.deploy.release import collector_artifact
from base.paths import otel_collector_config, otel_collector_dir
from cli.commands.observability.tests.converge_otel_collector.collector_harness import (
    RUNNER,
    CollectorUnit,
    MirrorOpener,
)
from cli.commands.observability.tests.converge_otel_collector.collector_harness import (
    collector_unit as collector_unit,
)
from cli.commands.observability.tests.scripted_http import Reply, serve
from cli.database.tests.command_inputs import command_runtime as command_runtime

ASSET = f"/otelcol-contrib_{collector_artifact.OTELCOL_CONTRIB_VERSION}_linux_amd64.tar.gz"


def test_a_missing_binary_is_fetched_through_the_units_opener_and_checked_against_the_pin(
    collector_unit: CollectorUnit,
) -> None:
    with serve({ASSET: Reply(body=b"not the pinned tarball")}) as mirror:
        unit = replace(collector_unit, open_url=MirrorOpener(mirror.url))
        with pytest.raises(RuntimeError, match="SHA256 mismatch"):
            unit.ensure(RUNNER)

    assert [(request.method, request.path) for request in mirror.received] == [("GET", ASSET)]
    assert not (otel_collector_dir(unit.home) / collector_artifact.BINARY_NAME).exists()
    assert not (otel_collector_dir(unit.home) / collector_artifact.VERSION_MARKER).exists()
    assert not otel_collector_config(unit.home).exists()


def test_a_flaky_mirror_is_retried_before_the_digest_is_checked(
    collector_unit: CollectorUnit,
) -> None:
    flaky = [Reply(status=503), Reply(close=True), Reply(body=b"still not the pinned tarball")]
    with serve({ASSET: flaky}) as mirror:
        unit = replace(collector_unit, open_url=MirrorOpener(mirror.url))
        with pytest.raises(RuntimeError, match="SHA256 mismatch"):
            unit.ensure(RUNNER)

    assert len(mirror.received) == 3
