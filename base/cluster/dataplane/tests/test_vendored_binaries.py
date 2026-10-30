"""Vendored relocatable Postgres resolution and hermetic extraction.

The real downloadable artifact is covered by ``scripts/ci/pgvector_runtime_smoke.py``
in the ``backend-pgvector-smoke`` CI job. This unit suite stays offline while
covering the checksum, extraction, executable, and idempotence contracts that
``meiva start`` depends on for a brew-free machine.
"""

from __future__ import annotations

import email.message
import hashlib
import io
import lzma
import subprocess
import tarfile
import urllib.error
import zipfile
from pathlib import Path

import pytest

from base.clock.tests.manual_timers import ManualTimers
from base.cluster.dataplane import pg_tools
from base.cluster.dataplane import runtime_binaries as rb
from base.host.net.tests.fake_url_opener import Answer, FakeUrlOpener
from base.log.tests.log_records import LogRecords


@pytest.fixture()
def isolated_runtime(tmp_path: Path) -> Path:
    """Point the runtime root (`$MEIVA_HOME/runtime`) at a tmp home, so a test never
    touches the real ~/.meiva/runtime."""
    return tmp_path / "home"


def test_pg_tool_prefers_vendored_dir(isolated_runtime: Path) -> None:
    # Absent vendored tree -> pg_tool falls back to the host (brew/apt) path.
    assert "runtime" not in str(pg_tools.pg_tool("initdb", home=isolated_runtime))

    # A present vendored tree wins.
    bin_dir = rb.vendored_pg_dir(isolated_runtime) / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "initdb").write_text("#!/bin/sh\n")
    assert pg_tools.pg_tool("initdb", home=isolated_runtime) == bin_dir / "initdb"
    assert rb.vendored_pg_bin_dir(isolated_runtime) == bin_dir


def _http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://repo1.invalid/x.jar", status, "boom", email.message.Message(), None
    )


def _opener_for(
    *answers: Answer | BaseException,
) -> FakeUrlOpener:
    opener = FakeUrlOpener()
    opener.on("https://", *answers)
    return opener


def test_download_retries_transient_answers_then_succeeds() -> None:
    """429/5xx from the mirror and socket-level failures back off and retry; the
    sha256 pin makes the eventual bytes trustworthy regardless of which attempt won."""
    timers = ManualTimers()
    opener = _opener_for(
        _http_error(429),
        _http_error(503),
        urllib.error.URLError("reset"),
        Answer(body=b"jar"),
    )
    assert (
        rb.download_with_opener("https://repo1.invalid/x.jar", open_url=opener, sleep=timers.sleep)
        == b"jar"
    )
    assert len(opener.urls) == 4
    assert timers.sleeps == [2, 4, 8]


@pytest.mark.parametrize("status", [403, 404, 429, 500, 502, 503, 504])
def test_download_retries_transient_http_then_succeeds(status: int) -> None:
    timers = ManualTimers()
    opener = _opener_for(_http_error(status), Answer(body=b"jar"))
    assert (
        rb.download_with_opener("https://repo1.invalid/x.jar", open_url=opener, sleep=timers.sleep)
        == b"jar"
    )
    assert len(opener.urls) == 2
    assert timers.sleeps == [2]


def test_download_ignores_retry_after() -> None:
    timers = ManualTimers()
    error = _http_error(429)
    error.headers["Retry-After"] = "60"
    opener = _opener_for(error, Answer(body=b"jar"))
    assert (
        rb.download_with_opener("https://repo1.invalid/x.jar", open_url=opener, sleep=timers.sleep)
        == b"jar"
    )
    assert timers.sleeps == [2]


@pytest.mark.parametrize("status", [400, 401])
def test_download_fails_fast_on_a_permanent_http_answer(
    status: int,
) -> None:
    """A non-transient 4xx (bad request) fails without retry."""
    timers = ManualTimers()
    error = _http_error(status)
    opener = _opener_for(error)
    with pytest.raises(RuntimeError) as caught:
        rb.download_with_opener("https://repo1.invalid/x.jar", open_url=opener, sleep=timers.sleep)
    assert (
        str(caught.value)
        == f"failed to download vendored Postgres from https://repo1.invalid/x.jar: {error}"
    )
    assert caught.value.__cause__ is error
    assert len(opener.urls) == 1
    assert timers.sleeps == []


@pytest.mark.parametrize("status", [403, 404, 429])
def test_download_gives_up_after_bounded_retries(status: int) -> None:
    timers = ManualTimers()
    opener = _opener_for(*([_http_error(status)] * rb.DOWNLOAD_ATTEMPTS))
    with pytest.raises(RuntimeError, match=str(status)):
        rb.download_with_opener("https://repo1.invalid/x.jar", open_url=opener, sleep=timers.sleep)
    assert len(opener.urls) == rb.DOWNLOAD_ATTEMPTS
    assert timers.sleeps == [2, 4, 8]


def test_download_preserves_transient_warning_and_final_error(
    loguru_records: LogRecords,
) -> None:
    timers = ManualTimers()
    url = "https://repo1.invalid/x.jar"
    errors = [urllib.error.URLError(f"reset {i}") for i in range(1, 5)]
    opener = _opener_for(*errors)

    with pytest.raises(RuntimeError) as caught:
        rb.download_with_opener(url, open_url=opener, sleep=timers.sleep)

    assert opener.urls == [url] * 4
    assert timers.sleeps == [2, 4, 8]
    assert loguru_records.messages("WARNING") == [
        f"[runtime] transient error fetching {url} (<urlopen error reset {i}>); "
        f"retry {i}/3 in {2**i}s"
        for i in range(1, 4)
    ]
    assert str(caught.value) == f"failed to download vendored Postgres from {url}: {errors[-1]}"
    assert caught.value.__cause__ is errors[-1]


def _synthetic_pg_jar(artifact_name: str) -> bytes:
    """A jar wrapping the minimal executable Postgres tree used by the test."""
    txz = io.BytesIO()
    with tarfile.open(fileobj=txz, mode="w:xz") as tar:
        for directory in ("bin", "lib", "share"):
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tar.addfile(info)
        initdb = b'#!/bin/sh\necho "17.4"\n'
        info = tarfile.TarInfo("bin/initdb")
        info.mode = 0o755
        info.size = len(initdb)
        tar.addfile(info, io.BytesIO(initdb))

    jar = io.BytesIO()
    with zipfile.ZipFile(jar, mode="w") as archive:
        archive.writestr(f"{artifact_name}.txz", txz.getvalue())
    return jar.getvalue()


def test_ensure_pg_binaries_hermetic_fixture(
    isolated_runtime: Path,
) -> None:
    artifact_name = "synthetic-postgres"
    synthetic_jar = _synthetic_pg_jar(artifact_name)
    key = rb.platform_key()
    artifacts = {
        key: (artifact_name, hashlib.sha256(synthetic_jar).hexdigest()),
    }
    opener = _opener_for(Answer(body=synthetic_jar))
    assert rb.vendored_pg_bin_dir(isolated_runtime) is None

    bin_dir = rb.ensure_pg_binaries(
        home=isolated_runtime,
        artifacts=artifacts,
        open_url=opener,
    )
    assert (bin_dir / "initdb").exists()
    assert rb.vendored_pg_bin_dir(isolated_runtime) == bin_dir

    # The relocatable binary runs standalone (no brew, from its extracted path).
    out = subprocess.run(  # noqa: S603 — resolved vendored initdb path + static flag
        [str(bin_dir / "initdb"), "--version"], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "17.4"

    # Idempotent: a second ensure is a no-op returning the same dir (no re-download).
    assert (
        rb.ensure_pg_binaries(
            home=isolated_runtime,
            artifacts=artifacts,
            open_url=opener,
        )
        == bin_dir
    )
    assert len(opener.urls) == 1


# ── pgvector injection unit coverage ───────────────────────────────────────
# The real artifacts are covered by scripts/ci/pgvector_runtime_smoke.py (the CI
# gate); these pin the extraction/injection mechanics with synthetic archives.


def _ar_member(name: str, payload: bytes) -> bytes:
    """One ar member: 60-byte header + payload, 2-byte aligned like real deb."""
    header = (f"{name:<16}{0:<12}{0:<6}{0:<6}{100644:<8}{len(payload):<10}`\n").encode("ascii")
    padding = b"\n" if len(payload) % 2 else b""
    return header + payload + padding


def _synthetic_deb() -> bytes:
    """A minimal deb-shaped ar archive carrying data.tar.xz with the three
    pgvector files at the PGDG install paths for the pinned major. Members
    carry the `./` prefix like a real dpkg-deb data tar."""
    major = rb.PG_VERSION.split(".", 1)[0]
    payloads = {
        f"./usr/lib/postgresql/{major}/lib/vector.so": b"SO",
        f"./usr/share/postgresql/{major}/extension/vector.control": b"CTRL",
        f"./usr/share/postgresql/{major}/extension/{rb.PGVECTOR_SQL}": b"SQL",
    }
    data_tar = io.BytesIO()
    with tarfile.open(fileobj=data_tar, mode="w") as tar:
        for name, content in payloads.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    deb = b"!<arch>\n"
    deb += _ar_member("debian-binary", b"2.0\n")
    deb += _ar_member("control.tar.xz", lzma.compress(b""))
    deb += _ar_member("data.tar.xz", lzma.compress(data_tar.getvalue()))
    return deb


def _synthetic_bottle() -> bytes:
    """A minimal bottle-shaped gzipped tar with the pgvector members at the
    formula install paths for the pinned major."""
    major = rb.PG_VERSION.split(".", 1)[0]
    prefix = f"pgvector/{rb.PGVECTOR_VERSION}/"
    payloads = {
        f"{prefix}lib/postgresql@{major}/vector.dylib": b"DYLIB",
        f"{prefix}share/postgresql@{major}/extension/vector.control": b"CTRL",
        f"{prefix}share/postgresql@{major}/extension/{rb.PGVECTOR_SQL}": b"SQL",
    }
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w:gz") as tar:
        for name, content in payloads.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return blob.getvalue()


def test_pgvector_deb_extraction_layout() -> None:
    assert rb.pgvector_extension_files(_synthetic_deb(), "linux-x86_64") == {
        "lib/postgresql/vector.so": b"SO",
        "share/postgresql/extension/vector.control": b"CTRL",
        f"share/postgresql/extension/{rb.PGVECTOR_SQL}": b"SQL",
    }


def test_pgvector_bottle_extraction_layout() -> None:
    assert rb.pgvector_extension_files(_synthetic_bottle(), "darwin-arm64") == {
        "lib/postgresql/vector.dylib": b"DYLIB",
        "share/postgresql/extension/vector.control": b"CTRL",
        f"share/postgresql/extension/{rb.PGVECTOR_SQL}": b"SQL",
    }


def test_pgvector_extraction_fails_fast_on_missing_member() -> None:
    """A renamed upstream layout must fail loudly, never inject a partial set."""
    major = rb.PG_VERSION.split(".", 1)[0]
    data_tar = io.BytesIO()
    with tarfile.open(fileobj=data_tar, mode="w") as tar:
        info = tarfile.TarInfo(f"./usr/lib/postgresql/{major}/lib/vector.so")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"SO"))
    deb = (
        b"!<arch>\n"
        + _ar_member("debian-binary", b"2.0\n")
        + _ar_member("data.tar.xz", lzma.compress(data_tar.getvalue()))
    )
    with pytest.raises(RuntimeError, match="member missing"):
        rb.pgvector_extension_files(deb, "linux-x86_64")


def test_ensure_pgvector_injects_and_is_idempotent(
    isolated_runtime: Path,
) -> None:
    (rb.vendored_pg_dir(isolated_runtime) / "bin").mkdir(parents=True)
    (rb.vendored_pg_dir(isolated_runtime) / "bin" / "initdb").write_text("#!/bin/sh\n")
    # The synthetic deb is this test's artifact, so the pin table is swapped for
    # one whose sha matches it — the pin check itself stays exercised.
    synthetic = _synthetic_deb()
    fake_url = "https://example.invalid/pgvector.deb"
    artifacts = {"linux-x86_64": (fake_url, hashlib.sha256(synthetic).hexdigest())}
    opener = FakeUrlOpener()
    opener.on(fake_url, Answer(body=synthetic), repeat=True)

    rb.ensure_pgvector(
        home=isolated_runtime,
        system="Linux",
        machine="x86_64",
        artifacts=artifacts,
        open_url=opener,
    )
    pg_dir = rb.vendored_pg_dir(isolated_runtime)
    assert (pg_dir / "lib/postgresql/vector.so").read_bytes() == b"SO"
    assert (pg_dir / "share/postgresql/extension/vector.control").read_bytes() == b"CTRL"
    assert (pg_dir / "share/postgresql/extension" / rb.PGVECTOR_SQL).read_bytes() == b"SQL"
    assert opener.urls == [fake_url]
    rb.ensure_pgvector(
        home=isolated_runtime,
        system="Linux",
        machine="x86_64",
        artifacts=artifacts,
        open_url=opener,
    )  # idempotent: detection files exist -> no second download
    assert len(opener.urls) == 1
    # Detection covers BOTH files: deleting the module alone re-injects
    # (a partially deleted injection must not silently survive).
    (pg_dir / "lib/postgresql/vector.so").unlink()
    rb.ensure_pgvector(
        home=isolated_runtime,
        system="Linux",
        machine="x86_64",
        artifacts=artifacts,
        open_url=opener,
    )
    assert len(opener.urls) == 2
    assert (pg_dir / "lib/postgresql/vector.so").read_bytes() == b"SO"


def test_ensure_pgvector_fails_on_checksum_mismatch(
    isolated_runtime: Path,
) -> None:
    (rb.vendored_pg_dir(isolated_runtime) / "bin" / "initdb").parent.mkdir(parents=True)
    (rb.vendored_pg_dir(isolated_runtime) / "bin" / "initdb").write_text("#!/bin/sh\n")
    opener = _opener_for(Answer(body=b"junk"))
    with pytest.raises(RuntimeError, match="sha256 mismatch"):
        rb.ensure_pgvector(
            home=isolated_runtime,
            system="Linux",
            machine="x86_64",
            open_url=opener,
        )


def test_ensure_pgvector_requires_pg_tree(isolated_runtime: Path) -> None:
    with pytest.raises(RuntimeError, match="ensure_pg_binaries"):
        rb.ensure_pgvector(home=isolated_runtime)


def test_download_ghcr_blob_fetches_token_then_blob() -> None:
    opener = FakeUrlOpener()
    auth_url = "https://ghcr.io/token?service=ghcr.io&scope=repository:homebrew/core/pgvector:pull"
    blob_url = "https://ghcr.io/v2/homebrew/core/pgvector/blobs/sha256:abc"
    opener.on(auth_url, Answer(body=b'{"token": "tok"}'))
    opener.on(blob_url, Answer(body=b"blob"))

    assert rb.download_ghcr_blob_with_opener(blob_url, open_url=opener) == b"blob"
    assert opener.calls[0].url == auth_url
    assert opener.calls[0].headers == {}
    assert opener.calls[1].url == blob_url
    assert opener.calls[1].headers == {"Authorization": "Bearer tok"}


def test_pgvector_platform_key_out_of_matrix() -> None:
    with pytest.raises(RuntimeError, match="linux/aarch64"):
        rb.pgvector_platform_key(system="Linux", machine="aarch64")


def test_intel_macos_has_no_pinned_pgvector_artifact() -> None:
    with pytest.raises(RuntimeError, match=r"no vendored pgvector available for darwin/x86_64"):
        rb.pgvector_platform_key(system="Darwin", machine="x86_64")
    assert set(rb.PGVECTOR_ARTIFACTS) == {"linux-x86_64", "darwin-arm64"}
