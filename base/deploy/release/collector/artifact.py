# meiva:note
# The shell of the pinned collector acquisition: it streams the release archive over the URL opener
# the caller hands in, hashes it as it arrives, retries under the budget, extracts and installs the
# binary and writes the version marker, reporting on stdout and stderr. Which asset a platform
# needs, what a digest mismatch, a spent budget or a failed attempt says and how long to wait
# between attempts are decided in `plan.py`.

from __future__ import annotations

import hashlib
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

from base.clock import Timers
from base.deploy.release.collector.plan import (
    BINARY_NAME,
    OTELCOL_CONTRIB_VERSION,
    VERSION_MARKER,
    DownloadLimits,
    DownloadStatus,
    beat_due,
    binary_member_name,
    content_length,
    release_asset,
)
from base.host.net.ports import UrlOpener
from base.host.net.resilience import Policy, retry


class DownloadProgress:
    def __init__(self, received: int = 0, *, deadline: float = 0.0) -> None:
        self.received = received
        self.deadline = deadline
        self._hasher = hashlib.sha256()

    def record(self, chunk: bytes) -> None:
        self.received += len(chunk)
        self._hasher.update(chunk)

    def hexdigest(self) -> str:
        return self._hasher.hexdigest()


# meiva:link base/deploy/release/docs/collector-artifact.meiva.okf.md
def stream_download(
    url: str,
    dest: Path,
    *,
    open_url: UrlOpener,
    timers: Timers,
    limits: DownloadLimits,
    progress: DownloadProgress,
) -> None:
    def _status() -> DownloadStatus:
        return DownloadStatus.of_stream(
            limits, progress.received, deadline_s=progress.deadline, now_s=timers.monotonic()
        )

    def _require_budget() -> DownloadStatus:
        status = _status()
        message = status.budget_message()
        if message is not None:
            raise TimeoutError(message)
        return status

    last_beat = 0.0
    total: int | None = None
    with (
        open_url(url, _require_budget().socket_timeout_s()) as resp,
        dest.open("wb") as fh,
    ):
        total = content_length(resp.headers.get("Content-Length"))
        while True:
            _require_budget()
            chunk = resp.read1(1 << 16)
            _require_budget()
            if not chunk:
                break
            fh.write(chunk)
            progress.record(chunk)
            status = _status()
            if beat_due(status.used_s, last_beat, limits.progress_interval_s):
                _report(status.progress_line(total))
                last_beat = status.used_s
    _report(_status().done_line())


def _download_policy(limits: DownloadLimits) -> Policy:
    return Policy(
        max_attempts=limits.attempts,
        backoff=limits.backoff,
        jitter="none",
        jitter_span=1.0,
        classify=lambda _exc: True,
        idempotent=True,
        respect_retry_after=False,
        on_final_failure=None,
    )


# meiva:link base/deploy/release/docs/collector-artifact.meiva.okf.md
def download_with_retry(
    url: str, tarball: Path, *, open_url: UrlOpener, timers: Timers, limits: DownloadLimits
) -> str:
    started = timers.monotonic()
    deadline = limits.deadline(started)
    attempt = 0
    progress = DownloadProgress()

    def _status() -> DownloadStatus:
        return DownloadStatus.of_download(
            limits, progress.received, started_s=started, now_s=timers.monotonic()
        )

    def _wait(seconds: float) -> None:
        wait = _status().wait_s(seconds)
        if wait is not None:
            timers.sleep(wait)

    # meiva:note
    # One attempt: any failure of it (a URLError, a TimeoutError, an OSError) is reported and left to
    # the retry loop.
    def _download_once() -> str:
        nonlocal attempt, progress
        attempt += 1
        progress = DownloadProgress(deadline=deadline)
        try:
            stream_download(
                url,
                tarball,
                open_url=open_url,
                timers=timers,
                limits=limits,
                progress=progress,
            )
        except Exception as exc:
            _report(_status().attempt_failure_line(attempt, exc), error=True)
            raise
        return progress.hexdigest()

    try:
        return retry(_download_policy(limits), sleep=_wait)(_download_once)
    except Exception as exc:
        raise RuntimeError(_status().give_up_message(url, attempt, exc)) from exc


# meiva:note
# Download + SHA256-verify + install the pinned tarball's binary into dest_dir.
def download_and_verify(
    tag: str, dest_dir: Path, *, open_url: UrlOpener, timers: Timers, limits: DownloadLimits
) -> None:
    asset = release_asset(tag)
    with tempfile.TemporaryDirectory() as tmp:
        tarball = Path(tmp) / "otelcol-contrib.tar.gz"
        _report(f"  · otel-collector: downloading {asset.url}")
        digest = download_with_retry(
            asset.url, tarball, timers=timers, open_url=open_url, limits=limits
        )
        mismatch = asset.mismatch_message(digest)
        if mismatch is not None:
            raise RuntimeError(mismatch)
        install_binary(tarball, dest_dir)
    (dest_dir / VERSION_MARKER).write_text(OTELCOL_CONTRIB_VERSION + "\n", encoding="utf-8")


# meiva:note
# Put the collector binary of `tarball` at `dest_dir/otelcol-contrib`, mode 0755.
# The binary is extracted into a staging directory inside `dest_dir` (the same filesystem, so
# the final move is a rename) and renamed over the destination in one step. An upgrade replaces
# the directory entry and never writes into the old file, which may be a running collector
# (Linux refuses to write to an executable in use, macOS would truncate it), and a failure
# part-way leaves the previous binary untouched.
def install_binary(tarball: Path, dest_dir: Path) -> None:
    with tarfile.open(tarball) as tf:
        members = tf.getmembers()
        name = binary_member_name([m.name for m in members])
        member = next(m for m in members if m.name == name)
        with tempfile.TemporaryDirectory(dir=dest_dir) as staging:
            tf.extract(member, path=staging, filter="data")
            staged = Path(staging) / member.name
            staged.chmod(0o755)
            shutil.move(staged, dest_dir / BINARY_NAME)


def _report(message: str, *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    stream.write(message + "\n")
    stream.flush()
