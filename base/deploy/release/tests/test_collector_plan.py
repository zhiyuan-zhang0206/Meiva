# meiva:note
# What the pinned collector acquisition decides, asserted as values: the release asset of a tag, the
# archive member that is the binary, how a download's budget, progress and failures read and how
# long to wait between attempts. No server runs and no file is read; `test_collector_artifact_*`
# drive the shell over real local servers and tarballs.

from __future__ import annotations

import pytest

from base.deploy.release.collector.plan import (
    OTELCOL_CONTRIB_VERSION,
    DownloadLimits,
    DownloadStatus,
    beat_due,
    binary_member_name,
    content_length,
    release_asset,
)

_LIMITS = DownloadLimits(
    total_budget_s=1800.0,
    socket_timeout_s=30.0,
    attempts=3,
    retry_backoff_s=5.0,
    progress_interval_s=15.0,
)


def _status(*, received: int = 2_500_000, used_s: float = 61.4, remaining_s: float = 90.0):
    return DownloadStatus(_LIMITS, received=received, used_s=used_s, remaining_s=remaining_s)


def test_a_tag_names_its_release_asset_and_pinned_digest() -> None:
    asset = release_asset("darwin_arm64")

    assert asset.tag == "darwin_arm64"
    assert asset.url == (
        "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/"
        f"v{OTELCOL_CONTRIB_VERSION}/otelcol-contrib_{OTELCOL_CONTRIB_VERSION}_darwin_arm64.tar.gz"
    )
    assert asset.sha256 == "d5e11974d2e4adac3cc001a25137aad52f6e77ec3034ba7735cac7c219a5f97a"


_PINNED = {
    "darwin_arm64": "d5e11974d2e4adac3cc001a25137aad52f6e77ec3034ba7735cac7c219a5f97a",
    "darwin_amd64": "91a6e7a0f5e1981986c897db4535de5ba8d7fc45d4205809aecb9ebebf68c71c",
    "linux_amd64": "fcc063749f730f8c21fe29f2d340ff174f5f1c5885bd3156fb6c985a3036fcc3",
    "linux_arm64": "ecf6a917e2b53beb1703b19a84bc4e112a2fcaba549639cf6b0ba58446bfd7d2",
}


@pytest.mark.parametrize(("tag", "digest"), sorted(_PINNED.items()))
def test_every_supported_tag_has_its_own_asset_and_pinned_digest(tag: str, digest: str) -> None:
    asset = release_asset(tag)

    assert asset.url.endswith(f"_{tag}.tar.gz")
    assert asset.sha256 == digest


def test_the_pinned_digests_are_distinct() -> None:
    assert len({release_asset(tag).sha256 for tag in _PINNED}) == len(_PINNED)


def test_the_download_is_announced_with_its_url() -> None:
    asset = release_asset("linux_arm64")

    assert asset.download_line() == f"  · otel-collector: downloading {asset.url}"


def test_a_tag_without_a_pinned_asset_is_refused() -> None:
    with pytest.raises(KeyError):
        release_asset("linux_386")


def test_a_download_with_the_pinned_digest_is_accepted() -> None:
    asset = release_asset("linux_amd64")

    assert asset.mismatch_message(asset.sha256) is None


def test_a_download_with_another_digest_is_refused_naming_both() -> None:
    asset = release_asset("linux_amd64")

    message = asset.mismatch_message("0" * 64)

    assert message == (
        f"otelcol-contrib {OTELCOL_CONTRIB_VERSION} linux_amd64 SHA256 mismatch: "
        f"got {'0' * 64}, expected {asset.sha256} — refusing to install"
    )


def test_the_binary_is_the_first_member_ending_with_its_name() -> None:
    names = ["LICENSE", "bin/otelcol-contrib", "otelcol-contrib", "otelcol-contrib.sha"]

    assert binary_member_name(names) == "bin/otelcol-contrib"


def test_an_archive_without_the_binary_is_refused() -> None:
    with pytest.raises(RuntimeError, match="no otelcol-contrib member"):
        binary_member_name(["LICENSE", "otelcol-contrib.sha"])


@pytest.mark.parametrize(
    ("header", "expected"),
    [("123", 123), ("0", 0), (None, 0), ("", 0), ("abc", None), ("12.5", None)],
)
def test_the_announced_size_is_the_header_or_unknown(header: str | None, expected: int | None):
    assert content_length(header) == expected


@pytest.mark.parametrize(
    ("elapsed", "last_beat", "due"),
    [(15.0, 0.0, True), (14.999, 0.0, False), (40.0, 25.0, True), (39.9, 25.0, False)],
)
def test_a_progress_line_is_due_once_the_interval_has_passed(
    elapsed: float, last_beat: float, due: bool
) -> None:
    assert beat_due(elapsed, last_beat, 15.0) is due


def test_the_wait_before_a_retry_grows_with_the_attempt() -> None:
    assert [_LIMITS.backoff(attempt) for attempt in range(3)] == [5.0, 10.0, 15.0]


def test_a_download_with_time_left_has_no_budget_error() -> None:
    assert _status(remaining_s=0.001).budget_message() is None


@pytest.mark.parametrize(
    ("remaining_s", "past"), [(-0.04, "0.0"), (-2.34, "2.3"), (-120.0, "120.0")]
)
def test_a_spent_budget_says_how_far_past_the_deadline_it_is(remaining_s: float, past: str):
    message = _status(received=4096, remaining_s=remaining_s).budget_message()

    assert message == (
        "otel-collector download exceeded its 1800s budget "
        f"(4096 bytes received, {past}s past the deadline)"
    )


def test_a_socket_operation_waits_the_configured_time_or_what_is_left() -> None:
    assert _status(remaining_s=500.0).socket_timeout_s() == 30.0
    assert _status(remaining_s=12.5).socket_timeout_s() == 12.5


def test_a_retry_wait_is_cut_to_the_budget_and_skipped_when_it_is_spent() -> None:
    assert _status(remaining_s=500.0).wait_s(10.0) == 10.0
    assert _status(remaining_s=4.0).wait_s(10.0) == 4.0
    assert _status(remaining_s=0.0).wait_s(10.0) is None
    assert _status(remaining_s=-3.0).wait_s(10.0) is None


def test_the_progress_line_shows_a_percentage_only_for_a_known_size() -> None:
    status = _status(received=2_500_000, used_s=61.4)

    assert status.progress_line(10_000_000) == "  · otel-collector: 2.5 MB (25%) in 61s"
    assert status.progress_line(None) == "  · otel-collector: 2.5 MB in 61s"
    assert status.progress_line(0) == "  · otel-collector: 2.5 MB in 61s"


def test_a_finished_download_closes_with_its_size_and_time() -> None:
    assert _status(received=12_300_000, used_s=7.6).done_line() == (
        "  · otel-collector: downloaded 12.3 MB in 8s"
    )


def test_a_failed_attempt_reports_what_is_left() -> None:
    line = _status(received=1024, used_s=33.2, remaining_s=1766.8).attempt_failure_line(
        2, OSError("reset by peer")
    )

    assert line == (
        "  ! otel-collector: download attempt 2/3 failed after 33s (1767s of the 1800s budget "
        "left, 1024 bytes received): reset by peer"
    )


def test_giving_up_reports_the_url_the_attempts_and_the_last_error() -> None:
    message = _status(received=0, used_s=95.0, remaining_s=-3.0).give_up_message(
        "https://example.invalid/t.tar.gz", 3, TimeoutError("slow")
    )

    assert message == (
        "failed to download otel-collector from https://example.invalid/t.tar.gz after 3 "
        "attempt(s) (95s used of the 1800s budget, 0s left, 0 bytes received by the last "
        "attempt): slow"
    )


def test_the_deadline_is_the_start_plus_the_budget() -> None:
    assert _LIMITS.deadline(100.0) == 1900.0


def test_a_stream_status_charges_the_time_since_the_deadline_was_set() -> None:
    status = DownloadStatus.of_stream(_LIMITS, 7, deadline_s=1900.0, now_s=160.0)

    assert (status.received, status.remaining_s, status.used_s) == (7, 1740.0, 60.0)


def test_a_stream_status_past_the_deadline_has_nothing_left() -> None:
    status = DownloadStatus.of_stream(_LIMITS, 7, deadline_s=1900.0, now_s=1905.0)

    assert (status.remaining_s, status.used_s) == (-5.0, 1805.0)
    assert status.budget_message() is not None


def test_a_download_status_counts_from_its_own_start() -> None:
    status = DownloadStatus.of_download(_LIMITS, 9, started_s=100.0, now_s=160.0)

    assert (status.received, status.used_s, status.remaining_s) == (9, 60.0, 1740.0)


def test_a_failed_attempt_past_the_budget_reports_nothing_left_not_a_negative() -> None:
    line = _status(received=1024, used_s=1803.0, remaining_s=-3.0).attempt_failure_line(
        1, OSError("x")
    )

    assert "(0s of the 1800s budget left" in line
