from __future__ import annotations

import hashlib
import io
import lzma
import shlex
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from base.cluster.dataplane import pg_runtime, runtime_binaries
from base.host.net.tests.fake_url_opener import Answer, FakeUrlOpener


def _synthetic_pg_jar(artifact_name: str) -> bytes:
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


def _ar_member(name: str, payload: bytes) -> bytes:
    header = (f"{name:<16}{0:<12}{0:<6}{0:<6}{100644:<8}{len(payload):<10}`\n").encode("ascii")
    padding = b"\n" if len(payload) % 2 else b""
    return header + payload + padding


def _synthetic_deb() -> bytes:
    major = runtime_binaries.PG_VERSION.split(".", 1)[0]
    payloads = {
        f"./usr/lib/postgresql/{major}/lib/vector.so": b"SO",
        f"./usr/share/postgresql/{major}/extension/vector.control": b"CTRL",
        f"./usr/share/postgresql/{major}/extension/{runtime_binaries.PGVECTOR_SQL}": b"SQL",
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


@pytest.fixture
def installed(tmp_path: Path) -> Path:
    bindir = tmp_path / "pg17" / "bin"
    bindir.mkdir(parents=True)
    extension = bindir.parent / "share" / "extension"
    extension.mkdir(parents=True)
    library = bindir.parent / "lib"
    library.mkdir()
    (extension / "vector.control").write_text("default_version = '0.8.6'\n")
    (extension / "vector--0.8.6.sql").write_text("-- fixture\n")
    suffix = ".dylib" if sys.platform == "darwin" else ".so"
    (library / f"vector{suffix}").touch()
    body = (
        '#!/bin/sh\ncase "$1" in\n'
        ' --version) echo "PostgreSQL 17.11";;\n'
        f" --bindir) echo {shlex.quote(str(bindir))};;\n"
        f" --sharedir) echo {shlex.quote(str(extension.parent))};;\n"
        f" --pkglibdir) echo {shlex.quote(str(library))};;\n"
        " *) exit 3;;\nesac\n"
    )
    for name in ("postgres", "initdb", "pg_ctl", "pg_config", "pg_dump", "pg_restore"):
        tool = bindir / name
        tool.write_text(body)
        tool.chmod(0o700)
    return bindir


@pytest.mark.parametrize("version", ["16.8", "18.0", "17.1beta1"])
def test_installed_wrong_major_or_unstable_version_fails_without_replacement(
    installed: Path, tmp_path: Path, version: str
) -> None:
    (installed / "postgres").write_text(f'#!/bin/sh\necho "PostgreSQL {version}"\n')
    with pytest.raises(RuntimeError, match="PostgreSQL 17 is required"):
        pg_runtime.ensure_pg_runtime(
            home=tmp_path,
            system="Linux",
            machine="aarch64",
            platform_bin_dir=installed,
            path_env="",
        )


@pytest.mark.parametrize("missing", ["vector.control", "vector--0.8.6.sql", "library"])
def test_installed_missing_extension_fails_without_replacement(
    installed: Path, tmp_path: Path, missing: str
) -> None:
    if missing == "library":
        next((installed.parent / "lib").iterdir()).unlink()
    else:
        (installed.parent / "share" / "extension" / missing).unlink()
    with pytest.raises(RuntimeError, match="Install pgvector"):
        pg_runtime.ensure_pg_runtime(
            home=tmp_path,
            system="Linux",
            machine="aarch64",
            platform_bin_dir=installed,
            path_env="",
        )


@pytest.mark.parametrize("script", ["vector--0.8.5.sql", "vector--0.8.5--0.8.6.sql"])
def test_pgvector_requires_install_script_matching_default_version(
    installed: Path, tmp_path: Path, script: str
) -> None:
    extension = installed.parent / "share" / "extension"
    (extension / "vector--0.8.6.sql").rename(extension / script)
    with pytest.raises(RuntimeError, match="Install pgvector"):
        pg_runtime.ensure_pg_runtime(
            home=tmp_path,
            system="Linux",
            machine="aarch64",
            platform_bin_dir=installed,
            path_env="",
        )


@pytest.mark.parametrize("duplicate", ["'missing'", "'0.8.5'", "unquoted"])
def test_pgvector_rejects_multiple_default_assignments(
    installed: Path, tmp_path: Path, duplicate: str
) -> None:
    control = installed.parent / "share" / "extension" / "vector.control"
    control.write_text(control.read_text() + f"default_version = {duplicate}\n")
    with pytest.raises(RuntimeError, match="Install pgvector"):
        pg_runtime.ensure_pg_runtime(
            home=tmp_path,
            system="Linux",
            machine="aarch64",
            platform_bin_dir=installed,
            path_env="",
        )


def test_mixed_installations_fail_before_data_mutation(installed: Path, tmp_path: Path) -> None:
    other = tmp_path / "pg18"
    other.mkdir()
    (other / "pg_ctl").write_text((installed / "pg_ctl").read_text())
    (other / "pg_ctl").chmod(0o700)
    (installed / "pg_ctl").unlink()

    with pytest.raises(RuntimeError, match="does not belong"):
        pg_runtime.ensure_pg_runtime(
            home=tmp_path,
            system="Linux",
            machine="aarch64",
            platform_bin_dir=installed,
            path_env=str(other),
        )


@pytest.mark.parametrize("existing_vendor", [False, True])
def test_supported_platform_provisions_the_pinned_tree_whatever_is_installed(
    installed: Path, tmp_path: Path, existing_vendor: bool
) -> None:
    vendor_home = tmp_path / "vendor-home"
    if existing_vendor:
        vendor_bin = runtime_binaries.vendored_pg_dir(vendor_home) / "bin"
        vendor_bin.mkdir(parents=True)
        (vendor_bin / "initdb").write_text("#!/bin/sh\n")

    jar = _synthetic_pg_jar("synthetic-postgres")
    deb = _synthetic_deb()
    pg_artifacts = {
        "linux-x86_64": ("synthetic-postgres", hashlib.sha256(jar).hexdigest()),
    }
    vec_url = "https://example.invalid/pgvector.deb"
    pgvector_artifacts = {
        "linux-x86_64": (vec_url, hashlib.sha256(deb).hexdigest()),
    }
    opener = FakeUrlOpener()
    opener.on("https://repo1.maven.org/", Answer(body=jar))
    opener.on(vec_url, Answer(body=deb))

    pg_runtime.ensure_pg_runtime(
        home=vendor_home,
        system="Linux",
        machine="x86_64",
        platform_bin_dir=installed,
        path_env="",
        pg_artifacts=pg_artifacts,
        pgvector_artifacts=pgvector_artifacts,
        open_url=opener,
    )
    pg_dir = runtime_binaries.vendored_pg_dir(vendor_home)
    assert (pg_dir / "bin" / "initdb").exists()
    assert (pg_dir / "lib/postgresql/vector.so").read_bytes() == b"SO"
    assert len(opener.urls) == (1 if existing_vendor else 2)


def test_platform_without_an_artifact_and_without_an_install_fails(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="no installed PostgreSQL 17"):
        pg_runtime.ensure_pg_runtime(
            home=tmp_path,
            system="Linux",
            machine="aarch64",
            platform_bin_dir=tmp_path / "missing",
            path_env="",
        )


def test_vendored_platform_support_follows_the_artifact_table() -> None:
    for system, machine, supported in (
        ("Linux", "x86_64", True),
        ("Darwin", "arm64", True),
        ("Linux", "aarch64", False),
    ):
        assert runtime_binaries.vendored_pg_supported(system=system, machine=machine) is supported
