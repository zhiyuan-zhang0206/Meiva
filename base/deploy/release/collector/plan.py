# meiva:note
# What the pinned collector acquisition decides, as values: which release asset a platform needs,
# its URL and pinned digest, which archive member is the binary, how a download's budget, progress
# and failures are worded, and how long to wait between attempts. `artifact.py` streams the
# download, installs the binary and reports.

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, assert_never

# meiva:note
# Pinned contrib version — re-validate against the deploy/lgtm backends (Tempo/Loki/Prometheus
# OTLP intake) when bumping.
OTELCOL_CONTRIB_VERSION = "0.162.0"

# meiva:note
# SHA256 of each supported platform's release tarball (opentelemetry-collector-releases v0.162.0
# checksums). Keyed by the platform tag used in the asset name.
_OTELCOL_CONTRIB_SHA256 = {
    "darwin_arm64": "d5e11974d2e4adac3cc001a25137aad52f6e77ec3034ba7735cac7c219a5f97a",
    "darwin_amd64": "91a6e7a0f5e1981986c897db4535de5ba8d7fc45d4205809aecb9ebebf68c71c",
    "linux_amd64": "fcc063749f730f8c21fe29f2d340ff174f5f1c5885bd3156fb6c985a3036fcc3",
    "linux_arm64": "ecf6a917e2b53beb1703b19a84bc4e112a2fcaba549639cf6b0ba58446bfd7d2",
}

_DOWNLOAD_URL = (
    "https://github.com/open-telemetry/opentelemetry-collector-releases/"
    f"releases/download/v{OTELCOL_CONTRIB_VERSION}/"
    "otelcol-contrib_{version}_{tag}.tar.gz"
)

VERSION_MARKER = "version"

BINARY_NAME = "otelcol-contrib"

_ARCH = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "amd64": "amd64"}


# meiva:note
# The release asset tag of a platform, or None when no binary is pinned for it.
#
# `system` is `Platform.system` ("darwin" or "linux") and `machine` the hardware name the OS
# reports ("arm64", "x86_64", "aarch64"; case does not matter). Tags follow the release assets:
# darwin_arm64 / darwin_amd64 / linux_amd64 / linux_arm64. Anything else (linux_386, ...) has no
# pinned binary: the sidecar is skipped and OTLP export auto-disables at the agent preflight.
# It decides from its two arguments and reads nothing; a `system` that is neither raises an
# AssertionError whatever the machine, instead of looking like an unsupported platform.
def platform_tag_for(system: Literal["darwin", "linux"], machine: str) -> str | None:
    match system:
        case "darwin" | "linux":
            arch = _ARCH.get(machine.lower())
            return None if arch is None else f"{system}_{arch}"
        case _:
            assert_never(system)


# meiva:note
# The pinned release asset of one platform tag: where it is downloaded from and the SHA256 the
# download must have.
@dataclass(frozen=True)
class ReleaseAsset:
    tag: str
    url: str
    sha256: str

    # meiva:note
    # The line that announces the download of this asset.
    def download_line(self) -> str:
        return f"  · otel-collector: downloading {self.url}"

    # meiva:note
    # Why a download with this digest is refused, or None when it is the pinned asset.
    def mismatch_message(self, digest: str) -> str | None:
        if digest == self.sha256:
            return None
        return (
            f"otelcol-contrib {OTELCOL_CONTRIB_VERSION} {self.tag} SHA256 mismatch: "
            f"got {digest}, expected {self.sha256} — refusing to install"
        )


# meiva:note
# The pinned release asset for a platform tag; a tag with no pinned asset is a KeyError.
def release_asset(tag: str) -> ReleaseAsset:
    return ReleaseAsset(
        tag=tag,
        url=_DOWNLOAD_URL.format(version=OTELCOL_CONTRIB_VERSION, tag=tag),
        sha256=_OTELCOL_CONTRIB_SHA256[tag],
    )


# meiva:note
# The member of the release archive that is the collector binary: the first whose name ends with
# the binary name. An archive without one is refused.
def binary_member_name(names: Sequence[str]) -> str:
    for name in names:
        if name.endswith(BINARY_NAME):
            return name
    raise RuntimeError(f"otelcol-contrib tarball has no {BINARY_NAME} member")


# meiva:note
# The size a response announces in its Content-Length header, or None when it announces none that
# parses.
def content_length(header: str | None) -> int | None:
    try:
        return int(header or 0)
    except (TypeError, ValueError):
        return None


# meiva:note
# Whether a progress line is due: the time since the last one reached the interval.
def beat_due(elapsed_s: float, last_beat_s: float, interval_s: float) -> bool:
    return elapsed_s - last_beat_s >= interval_s


# meiva:note
# How long one download may take and how it retries.
@dataclass(frozen=True)
class DownloadLimits:
    total_budget_s: float
    socket_timeout_s: float
    attempts: int
    retry_backoff_s: float
    progress_interval_s: float

    # meiva:note
    # The wait before the attempt after attempt number `attempt` (counting from zero).
    def backoff(self, attempt: int) -> float:
        return self.retry_backoff_s * (attempt + 1)

    # meiva:note
    # The instant a download that started at `started_s` must be over.
    def deadline(self, started_s: float) -> float:
        return started_s + self.total_budget_s


# meiva:note
# Where a download stands: its limits, the bytes received and the seconds used and left of the
# budget. The messages are worded here so a test reads them as values.
@dataclass(frozen=True)
class DownloadStatus:
    limits: DownloadLimits
    received: int
    used_s: float
    remaining_s: float

    # meiva:note
    # The status of one attempt's stream at `now_s`, against the deadline of the whole download:
    # what has been used of the budget is what the deadline no longer leaves.
    @classmethod
    def of_stream(
        cls, limits: DownloadLimits, received: int, *, deadline_s: float, now_s: float
    ) -> DownloadStatus:
        remaining = deadline_s - now_s
        return cls(
            limits, received, used_s=limits.total_budget_s - remaining, remaining_s=remaining
        )

    # meiva:note
    # The status of the whole download at `now_s`, which began at `started_s`.
    @classmethod
    def of_download(
        cls, limits: DownloadLimits, received: int, *, started_s: float, now_s: float
    ) -> DownloadStatus:
        return cls(
            limits,
            received,
            used_s=now_s - started_s,
            remaining_s=limits.deadline(started_s) - now_s,
        )

    # meiva:note
    # The error of a download past its budget, or None while time is left.
    def budget_message(self) -> str | None:
        if self.remaining_s > 0:
            return None
        return (
            f"otel-collector download exceeded its {self.limits.total_budget_s:.0f}s budget "
            f"({self.received} bytes received, "
            f"{-self.remaining_s:.1f}s past the deadline)"
        )

    # meiva:note
    # The timeout of the next socket operation: the configured one, or what is left of the budget
    # when that is less.
    def socket_timeout_s(self) -> float:
        return min(self.limits.socket_timeout_s, self.remaining_s)

    # meiva:note
    # How long to wait before the next attempt: `seconds`, cut to what is left of the budget, or
    # None when nothing is left.
    def wait_s(self, seconds: float) -> float | None:
        if self.remaining_s <= 0:
            return None
        return min(seconds, self.remaining_s)

    # meiva:note
    # The progress line of a download in flight; `total` is the announced size, if any.
    def progress_line(self, total: int | None) -> str:
        pct = f" ({self.received * 100 // total}%)" if total else ""
        return f"  · otel-collector: {self.received / 1e6:.1f} MB{pct} in {self.used_s:.0f}s"

    # meiva:note
    # The line that closes a finished download.
    def done_line(self) -> str:
        return f"  · otel-collector: downloaded {self.received / 1e6:.1f} MB in {self.used_s:.0f}s"

    # meiva:note
    # The line that reports attempt number `attempt` failing with `error`.
    def attempt_failure_line(self, attempt: int, error: BaseException) -> str:
        return (
            f"  ! otel-collector: download attempt {attempt}/{self.limits.attempts} failed after "
            f"{self.used_s:.0f}s ({max(self.remaining_s, 0.0):.0f}s of the "
            f"{self.limits.total_budget_s:.0f}s budget left, {self.received} bytes received): "
            f"{error}"
        )

    # meiva:note
    # The error that ends a download which used all its attempts.
    def give_up_message(self, url: str, attempt: int, error: BaseException) -> str:
        return (
            f"failed to download otel-collector from {url} after {attempt} attempt(s) "
            f"({self.used_s:.0f}s used of the {self.limits.total_budget_s:.0f}s budget, "
            f"{max(self.remaining_s, 0.0):.0f}s left, {self.received} bytes received by the last "
            f"attempt): {error}"
        )
