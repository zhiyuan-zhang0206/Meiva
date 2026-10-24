# meiva:link cli/commands/observability/docs/lgtm/lgtm.meiva.okf.md
# meiva:note
# The native LGTM archive download and install against real HTTP listeners on port 0.
#
# A mirror that answers 503, drops the connection, drips a body or never answers is a real
# listener the shell dials through its real opener, and the archive it installs is a real zip or
# tarball under `tmp_path`. What the shell waits between attempts and says is decided in
# `plans/lgtm/download_retry.py` and tested there; these cases prove the loop, the deadline and the
# install against sockets and files.

from __future__ import annotations

import hashlib
import http.server
import re
import socketserver
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from cli.commands.observability import lgtm_native
from cli.commands.observability.plans.lgtm.download_retry import DownloadLimits
from cli.commands.observability.tests.lgtm.lgtm_harness import tar_bytes, zip_bytes
from cli.commands.observability.tests.observability_inputs import FAST_DOWNLOADS
from cli.commands.observability.tests.scripted_http import Reply, serve

LIMITS = replace(FAST_DOWNLOADS, retry_backoff_s=0.01)


def limits(**changes: float) -> DownloadLimits:
    return replace(LIMITS, **changes)


class DripServer(socketserver.ThreadingTCPServer):
    # meiva:note
    # A mirror that sends `body_len` bytes in `chunk_size` pieces `interval_s` apart, so a client
    # that checks no deadline would follow it for as long as it drips. `close_delimited` omits the
    # Content-Length; `stop_after_s` ends the stream early; `hold_open_s` keeps the connection open
    # after the last byte.
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        *,
        body_len: int,
        chunk_size: int,
        interval_s: float,
        hold_open_s: float = 0.0,
        close_delimited: bool = False,
        stop_after_s: float | None = None,
    ) -> None:
        super().__init__(("127.0.0.1", 0), _DripHandler)
        self.body_len = body_len
        self.chunk_size = chunk_size
        self.interval_s = interval_s
        self.hold_open_s = hold_open_s
        self.close_delimited = close_delimited
        self.stop_after_s = stop_after_s
        self.bytes_sent = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/loki.zip"

    def __enter__(self) -> DripServer:
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.shutdown()
        self.server_close()


class _DripHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        server = cast(DripServer, self.server)
        started = time.monotonic()
        self.send_response(200)
        if server.close_delimited:
            self.send_header("Connection", "close")
        else:
            self.send_header("Content-Length", str(server.body_len))
        self.end_headers()
        sent = 0
        while sent < server.body_len:
            if server.stop_after_s is not None and time.monotonic() - started > server.stop_after_s:
                return
            size = min(server.chunk_size, server.body_len - sent)
            self.wfile.write(b"x" * size)
            self.wfile.flush()
            sent += size
            server.bytes_sent = sent
            time.sleep(server.interval_s)
        if server.hold_open_s:
            time.sleep(server.hold_open_s)


class SilentServer(socketserver.ThreadingTCPServer):
    # meiva:note
    # A mirror that accepts the connection and never answers, for `stop_after_s` seconds.
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *, stop_after_s: float) -> None:
        super().__init__(("127.0.0.1", 0), _SilentHandler)
        self.stop_after_s = stop_after_s

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/loki.zip"

    def __enter__(self) -> SilentServer:
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.shutdown()
        self.server_close()


class _SilentHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        time.sleep(cast(SilentServer, self.server).stop_after_s)


def asset_for(url: str, body: bytes, member: str = "") -> dict[str, str]:
    return {"url": url, "sha256": hashlib.sha256(body).hexdigest(), "member": member}


def test_three_dropped_requests_give_up_naming_the_url_the_attempts_and_the_last_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with serve({"/loki.zip": Reply(status=503)}) as mirror:
        url = f"{mirror.url}/loki.zip"
        with pytest.raises(RuntimeError) as caught:
            lgtm_native.download_with_retry(url, tmp_path / "loki.zip", limits=LIMITS)

    message = str(caught.value)
    assert message.startswith(
        f"failed to download native LGTM backend from {url} after 3 attempt(s)"
    )
    assert "of the 30s budget" in message
    assert "0 bytes received by the last attempt): HTTP Error 503" in message
    assert len(mirror.received) == 3
    err = capsys.readouterr().err
    assert [line.split(" failed")[0] for line in err.splitlines()] == [
        "  ! lgtm native: download attempt 1/3",
        "  ! lgtm native: download attempt 2/3",
        "  ! lgtm native: download attempt 3/3",
    ]


def test_a_mirror_that_recovers_is_followed_to_the_archive(tmp_path: Path) -> None:
    destination = tmp_path / "loki.zip"
    with serve({"/loki.zip": [Reply(status=503), Reply(close=True), Reply(body=b"ok")]}) as mirror:
        lgtm_native.download_with_retry(f"{mirror.url}/loki.zip", destination, limits=LIMITS)

    assert destination.read_bytes() == b"ok"
    assert len(mirror.received) == 3


def test_the_backoff_is_a_real_wait_that_grows_by_one_step_per_attempt(tmp_path: Path) -> None:
    started = time.monotonic()
    with serve({"/loki.zip": Reply(status=503)}) as mirror, pytest.raises(RuntimeError):
        lgtm_native.download_with_retry(
            f"{mirror.url}/loki.zip", tmp_path / "loki.zip", limits=limits(retry_backoff_s=0.2)
        )

    assert time.monotonic() - started >= 0.2 + 0.4


def test_a_wait_never_runs_past_the_deadline(tmp_path: Path) -> None:
    started = time.monotonic()
    budget = limits(total_budget_s=0.5, retry_backoff_s=30.0)
    with serve({"/loki.zip": Reply(status=503)}) as mirror, pytest.raises(RuntimeError) as caught:
        lgtm_native.download_with_retry(
            f"{mirror.url}/loki.zip", tmp_path / "loki.zip", limits=budget
        )

    assert time.monotonic() - started < 3.0
    assert "exceeded its 0s budget" in str(caught.value.__cause__)
    assert len(mirror.received) == 1


@pytest.mark.parametrize("close_delimited", [False, True], ids=["content-length", "no-length"])
def test_a_dripping_mirror_cannot_outlive_the_asset_deadline(
    tmp_path: Path, close_delimited: bool
) -> None:
    budget = limits(total_budget_s=0.5, socket_timeout_s=1.0, retry_backoff_s=0.05)
    drip = DripServer(
        body_len=1 << 20,
        chunk_size=1,
        interval_s=0.02,
        close_delimited=close_delimited,
        stop_after_s=3.0,
    )
    started = time.monotonic()
    with drip, pytest.raises(RuntimeError, match="budget") as caught:
        lgtm_native.download_with_retry(drip.url, tmp_path / "loki.zip", limits=budget)

    elapsed = time.monotonic() - started
    assert elapsed >= budget.total_budget_s
    assert elapsed <= budget.total_budget_s + budget.socket_timeout_s + 0.3
    assert isinstance(caught.value.__cause__, TimeoutError)
    assert drip.bytes_sent < drip.body_len


def test_a_connection_that_never_sends_headers_ends_within_the_socket_guard(tmp_path: Path) -> None:
    budget = limits(total_budget_s=0.6, socket_timeout_s=0.2, retry_backoff_s=5.0)
    server = SilentServer(stop_after_s=3.0)
    started = time.monotonic()
    with server, pytest.raises(RuntimeError, match="budget") as caught:
        lgtm_native.download_with_retry(server.url, tmp_path / "loki.zip", limits=budget)

    assert time.monotonic() - started <= budget.total_budget_s + budget.socket_timeout_s + 0.3
    assert isinstance(caught.value.__cause__, TimeoutError)


def test_a_body_that_closes_after_the_deadline_is_not_a_success(tmp_path: Path) -> None:
    budget = limits(total_budget_s=1.0, socket_timeout_s=1.0)
    drip = DripServer(
        body_len=2,
        chunk_size=1,
        interval_s=0.5,
        hold_open_s=0.2,
        close_delimited=True,
        stop_after_s=3.0,
    )
    destination = tmp_path / "loki.zip"
    with drip, pytest.raises(TimeoutError, match="budget"):
        lgtm_native.stream_download(
            drip.url,
            destination,
            limits=budget,
            deadline=time.monotonic() + budget.total_budget_s,
        )

    assert destination.stat().st_size == drip.body_len
    assert drip.bytes_sent == drip.body_len


def test_the_progress_heartbeat_follows_the_configured_interval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    budget = limits(total_budget_s=30.0, progress_interval_s=0.1)
    drip = DripServer(body_len=10, chunk_size=1, interval_s=0.06)
    destination = tmp_path / "loki.zip"
    with drip:
        lgtm_native.stream_download(
            drip.url, destination, limits=budget, deadline=time.monotonic() + 30.0
        )

    assert destination.read_bytes() == b"x" * 10
    out = capsys.readouterr().out
    assert re.search(r"  · lgtm native: 0\.0 MB \(\d+%\) in \d+s\n", out)
    assert re.search(r"  · lgtm native: downloaded 0\.0 MB in \d+s\n\Z", out)


def test_an_archive_with_the_wrong_sha256_is_refused_and_leaves_no_marker(tmp_path: Path) -> None:
    body = b"untrusted archive"
    with serve({"/loki.zip": Reply(body=body)}) as mirror:
        asset = {"url": f"{mirror.url}/loki.zip", "sha256": "0" * 64, "member": "loki-bin"}
        with pytest.raises(RuntimeError, match="SHA256 mismatch"):
            lgtm_native.download_and_verify(
                "loki", "3.7.6", asset, tmp_path / "native", tag="linux_amd64", limits=LIMITS
            )

    assert not (tmp_path / "native" / "version-loki").exists()
    assert not (tmp_path / "native" / "bin").exists()


def test_a_verified_executable_is_installed_with_its_version_and_platform_markers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = zip_bytes("loki-linux-amd64", b"binary")
    native = tmp_path / "native"
    with serve({"/loki.zip": Reply(body=body)}) as mirror:
        lgtm_native.download_and_verify(
            "loki",
            "3.7.6",
            asset_for(f"{mirror.url}/loki.zip", body, "loki-linux-amd64"),
            native,
            tag="linux_amd64",
            limits=LIMITS,
        )

    assert (native / "bin/loki").read_bytes() == b"binary"
    assert (native / "bin/loki").stat().st_mode & 0o777 == 0o755
    assert (native / "version-loki").read_text(encoding="utf-8") == "3.7.6\n"
    assert (native / "platform-loki").read_text(encoding="utf-8") == "linux_amd64\n"
    captured = capsys.readouterr()
    assert captured.err == ""
    assert re.fullmatch(
        rf"  · lgtm native: downloading loki 3.7.6 from .+\n"
        rf"  · lgtm native: downloaded {len(body) / 1e6:.1f} MB in \d+s\n",
        captured.out,
    )


def test_a_tarball_member_is_installed_at_the_backends_binary_path(tmp_path: Path) -> None:
    member = "prometheus-3.13.2.linux-amd64/prometheus"
    body = tar_bytes({member: b"prometheus", "prometheus-3.13.2.linux-amd64/LICENSE": b"text"})
    native = tmp_path / "native"
    with serve({"/prometheus.tar.gz": Reply(body=body)}) as mirror:
        lgtm_native.download_and_verify(
            "prometheus",
            "3.13.2",
            asset_for(f"{mirror.url}/prometheus.tar.gz", body, member),
            native,
            tag="linux_amd64",
            limits=LIMITS,
        )

    assert (native / "bin/prometheus").read_bytes() == b"prometheus"
    assert not (native / "bin/LICENSE").exists()


def test_an_archive_without_the_expected_member_is_refused(tmp_path: Path) -> None:
    body = tar_bytes({"prometheus-3.13.2.linux-amd64/LICENSE": b"text"})
    with (
        serve({"/prometheus.tar.gz": Reply(body=body)}) as mirror,
        pytest.raises(RuntimeError, match=r"archive has no .*/prometheus member"),
    ):
        lgtm_native.download_and_verify(
            "prometheus",
            "3.13.2",
            asset_for(
                f"{mirror.url}/prometheus.tar.gz", body, "prometheus-3.13.2.linux-amd64/prometheus"
            ),
            tmp_path / "native",
            tag="linux_amd64",
            limits=LIMITS,
        )

    assert not (tmp_path / "native" / "version-prometheus").exists()


def test_a_release_tree_is_installed_without_its_top_level_directory(tmp_path: Path) -> None:
    body = tar_bytes({"grafana-13.2.3/bin/grafana": b"grafana", "grafana-13.2.3/conf/x.ini": b"x"})
    native = tmp_path / "native"
    (native / "grafana-home").mkdir(parents=True)
    (native / "grafana-home" / "previous-install").write_text("old", encoding="utf-8")
    with serve({"/grafana.tar.gz": Reply(body=body)}) as mirror:
        lgtm_native.download_and_verify(
            "grafana",
            "13.2.3",
            asset_for(f"{mirror.url}/grafana.tar.gz", body),
            native,
            tag="linux_amd64",
            limits=LIMITS,
        )

    assert (native / "grafana-home/bin/grafana").read_bytes() == b"grafana"
    assert (native / "grafana-home/conf/x.ini").read_bytes() == b"x"
    assert not (native / "grafana-home/previous-install").exists()
    assert (native / "version-grafana").read_text(encoding="utf-8") == "13.2.3\n"


def test_a_release_tree_with_two_top_level_directories_is_refused(tmp_path: Path) -> None:
    body = tar_bytes({"a/bin/grafana": b"1", "b/bin/grafana": b"2"})
    asset_url = "/grafana.tar.gz"
    with (
        serve({asset_url: Reply(body=body)}) as mirror,
        pytest.raises(RuntimeError, match="one top-level directory"),
    ):
        lgtm_native.download_and_verify(
            "grafana",
            "13.2.3",
            asset_for(f"{mirror.url}{asset_url}", body),
            tmp_path / "native",
            tag="linux_amd64",
            limits=LIMITS,
        )

    assert not (tmp_path / "native" / "version-grafana").exists()
