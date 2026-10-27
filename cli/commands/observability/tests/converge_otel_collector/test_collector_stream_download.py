# meiva:note
# The pinned collector's streaming download against a real listener on port 0, through the system
# URL opener. The function under test is `base.deploy.release.collector_artifact.stream_download`;
# the test belongs to that owner's tests and lives here until it moves.

from __future__ import annotations

import re
import socket
from pathlib import Path

import pytest

from base.clock import SYSTEM_TIMERS
from base.deploy.release.collector import artifact as artifact
from base.deploy.release.collector import plan as collector_plan
from base.host.net.ports import SYSTEM_URL_OPENER
from cli.commands.observability.tests.scripted_http import Reply, serve

LIMITS = collector_plan.DownloadLimits(
    total_budget_s=60.0,
    socket_timeout_s=30.0,
    attempts=3,
    retry_backoff_s=5.0,
    progress_interval_s=15.0,
)


def _stream(url: str, dest: Path, limits: collector_plan.DownloadLimits) -> None:
    artifact.stream_download(
        url,
        dest,
        timers=SYSTEM_TIMERS,
        open_url=SYSTEM_URL_OPENER,
        limits=limits,
        progress=artifact.DownloadProgress(
            deadline=SYSTEM_TIMERS.monotonic() + limits.total_budget_s
        ),
    )


def test_stream_download_writes_all_chunks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = b"x" * (4 << 20)
    dest = tmp_path / "t.tar.gz"

    with serve({"/t.tar.gz": Reply(body=payload)}) as served:
        _stream(f"{served.url}/t.tar.gz", dest, LIMITS)

    assert dest.read_bytes() == payload
    assert [(request.method, request.path) for request in served.received] == [("GET", "/t.tar.gz")]
    assert re.search(r"downloaded 4\.2 MB in \d+s", capsys.readouterr().out)


def test_stream_download_gives_up_on_a_server_that_never_answers_within_the_socket_timeout(
    tmp_path: Path,
) -> None:
    limits = collector_plan.DownloadLimits(
        total_budget_s=60.0,
        socket_timeout_s=0.2,
        attempts=1,
        retry_backoff_s=0.0,
        progress_interval_s=15.0,
    )
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen(1)
        url = f"http://127.0.0.1:{silent.getsockname()[1]}/t.tar.gz"

        with pytest.raises(TimeoutError):
            _stream(url, tmp_path / "t.tar.gz", limits)
