from __future__ import annotations

import hashlib
import io
import json
import lzma
import shutil
import tarfile
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path

from base.host.net.ports import SYSTEM_URL_OPENER, UrlOpener
from base.host.net.resilience import Policy, retry
from base.log import logger
from base.native_process.os_platform import Platform

__all__ = [
    "DOWNLOAD_ATTEMPTS",
    "PGVECTOR_ARTIFACTS",
    "PGVECTOR_SQL",
    "PGVECTOR_VERSION",
    "PG_ARTIFACTS",
    "PG_VERSION",
    "download",
    "download_ghcr_blob",
    "download_ghcr_blob_with_opener",
    "download_with_opener",
    "ensure_pg_binaries",
    "ensure_pgvector",
    "pgvector_extension_files",
    "pgvector_platform_key",
    "platform_key",
    "resolve_host_platform",
    "runtime_root",
    "vendored_pg_bin_dir",
    "vendored_pg_dir",
    "vendored_pg_supported",
]

PG_VERSION = "17.11.0"

_MAVEN_BASE = "https://repo1.maven.org/maven2/io/zonky/test/postgres"

PG_ARTIFACTS: dict[str, tuple[str, str]] = {
    "darwin": (
        "embedded-postgres-binaries-darwin-arm64v8",
        "a1c2786acb0c398f9b2d76806fc52f5dc8b222cbc8e9383a9b9702084daaf3a5",
    ),
    "linux-x86_64": (
        "embedded-postgres-binaries-linux-amd64",
        "0dd7b72b6f335b8ecfb355fa24c5781e8a93edd09880bb77eb52ebbf29b3e96d",
    ),
}

PGVECTOR_VERSION = "0.8.7"
PGVECTOR_SQL = f"vector--{PGVECTOR_VERSION}.sql"
_PGVECTOR_SQL = PGVECTOR_SQL

PGVECTOR_ARTIFACTS: dict[str, tuple[str, str]] = {
    "linux-x86_64": (
        "https://apt.postgresql.org/pub/repos/apt/pool/main/p/pgvector/"
        "postgresql-17-pgvector_0.8.7-1.pgdg12+1_amd64.deb",
        "acb5569b6a5bb71d5968205a4da15911ff3b2a15a57a2a9d765560882f53e3ea",
    ),
    "darwin-arm64": (
        "https://ghcr.io/v2/homebrew/core/pgvector/blobs/"
        "sha256:326bc17440a773b75b054d83d7905b46b370b1a1b6543fff077186b5a632253b",
        "326bc17440a773b75b054d83d7905b46b370b1a1b6543fff077186b5a632253b",
    ),
}


def resolve_host_platform(
    *, system: str | None = None, machine: str | None = None
) -> tuple[str, str]:
    if system is not None and machine is not None:
        return system, machine
    current = Platform.current()
    default_system = "Darwin" if current.system == "darwin" else "Linux"
    return (
        system if system is not None else default_system,
        machine if machine is not None else current.machine,
    )


def platform_key(*, system: str | None = None, machine: str | None = None) -> str:
    sys_name, arch = resolve_host_platform(system=system, machine=machine)
    if sys_name == "Darwin":
        return "darwin"
    if sys_name == "Linux":
        if arch in ("x86_64", "amd64"):
            return "linux-x86_64"
        raise RuntimeError(f"no vendored Postgres available for linux/{arch}")
    raise RuntimeError(f"no vendored Postgres available for {sys_name}")


def vendored_pg_supported(*, system: str | None = None, machine: str | None = None) -> bool:
    try:
        return platform_key(system=system, machine=machine) in PG_ARTIFACTS
    except RuntimeError:
        return False


def runtime_root(home: Path) -> Path:
    return home / "runtime"


def vendored_pg_dir(home: Path, *, pg_version: str = PG_VERSION) -> Path:
    return runtime_root(home) / "pg" / pg_version


def vendored_pg_bin_dir(home: Path, *, pg_version: str = PG_VERSION) -> Path | None:
    bin_dir = vendored_pg_dir(home, pg_version=pg_version) / "bin"
    return bin_dir if (bin_dir / "initdb").exists() else None


def ensure_pg_binaries(
    *,
    home: Path,
    system: str | None = None,
    machine: str | None = None,
    artifacts: Mapping[str, tuple[str, str]] | None = None,
    pg_version: str = PG_VERSION,
    open_url: UrlOpener | None = None,
) -> Path:
    pg_dir = vendored_pg_dir(home, pg_version=pg_version)
    bin_dir = pg_dir / "bin"
    if (bin_dir / "initdb").exists():
        return bin_dir

    key = platform_key(system=system, machine=machine)
    table = artifacts if artifacts is not None else PG_ARTIFACTS
    artifact, expected_sha = table[key]
    url = f"{_MAVEN_BASE}/{artifact}/{pg_version}/{artifact}-{pg_version}.jar"
    logger.info(f"[runtime] fetching vendored Postgres {pg_version} ({key}) from {url}")
    jar_bytes = (
        download_with_opener(url, open_url=open_url) if open_url is not None else download(url)
    )

    actual_sha = hashlib.sha256(jar_bytes).hexdigest()
    if actual_sha != expected_sha:
        raise RuntimeError(
            f"vendored Postgres jar sha256 mismatch for {artifact}-{pg_version}: "
            f"expected {expected_sha}, got {actual_sha}"
        )

    _extract_pg(jar_bytes, pg_dir)
    logger.info(f"[runtime] vendored Postgres {pg_version} ready at {pg_dir}")
    return bin_dir


DOWNLOAD_ATTEMPTS = 4
_TRANSIENT_HTTP = frozenset({403, 404, 429, 500, 502, 503, 504})


def _is_transient_download_error(exc: Exception) -> bool:
    if not isinstance(exc, urllib.error.URLError):
        return False
    status = exc.code if isinstance(exc, urllib.error.HTTPError) else None
    return status is None or status in _TRANSIENT_HTTP


def _download_backoff(attempt: int) -> float:
    return float(2 ** (attempt + 1))


_DOWNLOAD_POLICY = Policy(
    max_attempts=DOWNLOAD_ATTEMPTS,
    backoff=_download_backoff,
    jitter="none",
    jitter_span=1.0,
    classify=_is_transient_download_error,
    idempotent=True,
    respect_retry_after=False,
    on_final_failure=None,
)


def download_with_opener(
    url: str,
    *,
    open_url: UrlOpener,
    headers: dict[str, str] | None = None,
    what: str = "vendored Postgres",
    policy: Policy = _DOWNLOAD_POLICY,
) -> bytes:
    attempt = 0

    def _fetch_once() -> bytes:
        nonlocal attempt
        attempt += 1
        try:
            # meiva:note
            # `url` comes from the pinned HTTPS artifact tables (`PG_ARTIFACTS`,
            # `PGVECTOR_ARTIFACTS`, `WALG_ARTIFACTS`), and every downloaded blob
            # is SHA-256 verified before extraction or execution.
            request = urllib.request.Request(url, headers=headers or {})  # noqa: S310
            with open_url(request, 120.0) as resp:
                data: bytes = resp.read()
                return data
        except urllib.error.URLError as exc:
            if attempt < DOWNLOAD_ATTEMPTS and _is_transient_download_error(exc):
                delay = 2**attempt
                logger.warning(
                    f"[runtime] transient error fetching {url} ({exc}); "
                    f"retry {attempt}/{DOWNLOAD_ATTEMPTS - 1} in {delay}s"
                )
            raise

    try:
        return retry(policy)(_fetch_once)
    except urllib.error.URLError as exc:
        raise RuntimeError(f"failed to download {what} from {url}: {exc}") from exc


def download(
    url: str, *, headers: dict[str, str] | None = None, what: str = "vendored Postgres"
) -> bytes:
    return download_with_opener(url, open_url=SYSTEM_URL_OPENER, headers=headers, what=what)


def _extract_pg(jar_bytes: bytes, target: Path) -> None:
    import zipfile

    with zipfile.ZipFile(io.BytesIO(jar_bytes)) as jar:
        txz_names = [n for n in jar.namelist() if n.endswith(".txz")]
        if len(txz_names) != 1:
            raise RuntimeError(f"expected exactly one .txz in the Postgres jar, found {txz_names}")
        txz_bytes = jar.read(txz_names[0])

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(target.name + ".tmp")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(txz_bytes), mode="r:xz") as tar:
        tar.extractall(staging, filter="data")
    shutil.rmtree(target, ignore_errors=True)
    staging.rename(target)


def pgvector_platform_key(*, system: str | None = None, machine: str | None = None) -> str:
    sys_name, arch = resolve_host_platform(system=system, machine=machine)
    if sys_name == "Darwin":
        if arch == "arm64":
            return "darwin-arm64"
        raise RuntimeError(f"no vendored pgvector available for darwin/{arch}")
    if sys_name == "Linux":
        if arch in ("x86_64", "amd64"):
            return "linux-x86_64"
        raise RuntimeError(f"no vendored pgvector available for linux/{arch}")
    raise RuntimeError(f"no vendored pgvector available for {sys_name}")


def ensure_pgvector(
    *,
    home: Path,
    system: str | None = None,
    machine: str | None = None,
    artifacts: Mapping[str, tuple[str, str]] | None = None,
    pg_version: str = PG_VERSION,
    pgvector_version: str = PGVECTOR_VERSION,
    open_url: UrlOpener | None = None,
) -> None:
    pg_dir = vendored_pg_dir(home, pg_version=pg_version)
    key = pgvector_platform_key(system=system, machine=machine)
    sql_name = f"vector--{pgvector_version}.sql"
    module_rel = (
        "lib/postgresql/vector.dylib" if key.startswith("darwin") else "lib/postgresql/vector.so"
    )
    if (pg_dir / "share/postgresql/extension" / sql_name).exists() and (
        pg_dir / module_rel
    ).exists():
        return
    if not (pg_dir / "bin" / "initdb").exists():
        raise RuntimeError(
            f"vendored Postgres tree missing at {pg_dir} — ensure_pg_binaries() must run first"
        )
    table = artifacts if artifacts is not None else PGVECTOR_ARTIFACTS
    url, expected_sha = table[key]
    logger.info(f"[runtime] fetching pgvector {pgvector_version} ({key}) from {url}")
    if url.startswith("https://ghcr.io/"):
        blob = (
            download_ghcr_blob_with_opener(url, open_url=open_url)
            if open_url is not None
            else download_ghcr_blob(url)
        )
    else:
        blob = (
            download_with_opener(url, open_url=open_url) if open_url is not None else download(url)
        )
    actual_sha = hashlib.sha256(blob).hexdigest()
    if actual_sha != expected_sha:
        raise RuntimeError(
            f"pgvector artifact sha256 mismatch for {key}: "
            f"expected {expected_sha}, got {actual_sha}"
        )
    for relpath, content in pgvector_extension_files(
        blob, key, pg_version=pg_version, pgvector_version=pgvector_version
    ).items():
        target = pg_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.with_name(f".{target.name}.tmp")
        staging.write_bytes(content)
        staging.replace(target)
    logger.info(f"[runtime] pgvector {pgvector_version} injected into {pg_dir}")


def pgvector_extension_files(
    blob: bytes,
    key: str,
    *,
    pg_version: str = PG_VERSION,
    pgvector_version: str = PGVECTOR_VERSION,
) -> dict[str, bytes]:
    major = pg_version.split(".", 1)[0]
    if key.startswith("darwin"):
        return _pgvector_files_from_bottle(blob, major, pgvector_version=pgvector_version)
    return _pgvector_files_from_deb(blob, major, pgvector_version=pgvector_version)


def _pgvector_files_from_bottle(
    blob: bytes, major: str, *, pgvector_version: str = PGVECTOR_VERSION
) -> dict[str, bytes]:
    prefix = f"pgvector/{pgvector_version}/"
    sql_name = f"vector--{pgvector_version}.sql"
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        return {
            "lib/postgresql/vector.dylib": _tar_member_bytes(
                tar, f"{prefix}lib/postgresql@{major}/vector.dylib"
            ),
            "share/postgresql/extension/vector.control": _tar_member_bytes(
                tar, f"{prefix}share/postgresql@{major}/extension/vector.control"
            ),
            f"share/postgresql/extension/{sql_name}": _tar_member_bytes(
                tar, f"{prefix}share/postgresql@{major}/extension/{sql_name}"
            ),
        }


def _pgvector_files_from_deb(
    blob: bytes, major: str, *, pgvector_version: str = PGVECTOR_VERSION
) -> dict[str, bytes]:
    data_member = _deb_data_member(blob)
    if not data_member.startswith(b"\xfd\x37\x7a\x58\x5a\x00"):
        raise RuntimeError(
            "pgvector deb data member is not xz-compressed — the artifact layout "
            "changed; re-pin the deb and its extraction"
        )
    sql_name = f"vector--{pgvector_version}.sql"
    with tarfile.open(fileobj=io.BytesIO(lzma.decompress(data_member)), mode="r:") as tar:
        return {
            "lib/postgresql/vector.so": _tar_member_bytes(
                tar, f"usr/lib/postgresql/{major}/lib/vector.so"
            ),
            "share/postgresql/extension/vector.control": _tar_member_bytes(
                tar, f"usr/share/postgresql/{major}/extension/vector.control"
            ),
            f"share/postgresql/extension/{sql_name}": _tar_member_bytes(
                tar, f"usr/share/postgresql/{major}/extension/{sql_name}"
            ),
        }


def _deb_data_member(deb: bytes) -> bytes:
    if not deb.startswith(b"!<arch>\n"):
        raise RuntimeError("pgvector artifact is not an ar archive (deb)")
    pos = 8
    while pos + 60 <= len(deb):
        name = deb[pos : pos + 16].decode("ascii", "replace").strip().rstrip("/")
        size_field = deb[pos + 48 : pos + 58].decode("ascii", "replace").strip()
        if not size_field.isdigit():
            raise RuntimeError("corrupt ar member header in the pgvector deb")
        size = int(size_field)
        member = deb[pos + 60 : pos + 60 + size]
        if name in ("data.tar.xz", "data.tar.zst"):
            return member
        pos += 60 + size + (size % 2)
    raise RuntimeError("no data.tar member in the pgvector deb")


def _tar_member_bytes(tar: tarfile.TarFile, name: str) -> bytes:
    member = None
    for candidate in (name, f"./{name}"):
        try:
            member = tar.getmember(candidate)
            break
        except KeyError:
            continue
    if member is None:
        raise RuntimeError(f"pgvector artifact member missing: {name}")
    extracted = tar.extractfile(member)
    if extracted is None:
        raise RuntimeError(f"pgvector artifact member is not a regular file: {name}")
    return extracted.read()


def download_ghcr_blob_with_opener(
    url: str,
    *,
    open_url: UrlOpener,
    policy: Policy = _DOWNLOAD_POLICY,
) -> bytes:
    repository = url[len("https://ghcr.io/v2/") :].split("/blobs/", 1)[0]
    token_json = download_with_opener(
        f"https://ghcr.io/token?service=ghcr.io&scope=repository:{repository}:pull",
        open_url=open_url,
        policy=policy,
    )
    token = json.loads(token_json).get("token")
    if not token:
        raise RuntimeError(f"ghcr.io token endpoint answered without a token for {repository}")
    return download_with_opener(
        url,
        open_url=open_url,
        headers={"Authorization": f"Bearer {token}"},
        policy=policy,
    )


def download_ghcr_blob(url: str) -> bytes:
    return download_ghcr_blob_with_opener(url, open_url=SYSTEM_URL_OPENER)
