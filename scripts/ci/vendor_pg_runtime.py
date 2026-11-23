#!/usr/bin/env python
"""Select the pinned Postgres and pgvector runtime that CI test sessions link."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from base.cluster.dataplane import runtime_binaries as rb
from base.cluster.dataplane.artifact_fetch import RUNTIME_PINS_FILENAME, runtime_pin_manifest
from base.host.boundary.options import options
from base.host.net.ports import UrlOpener
from base.host.net.resilience import retry_sleep
from base.native_process.os_platform import Platform
from base.paths import meiva_home

_LEGACY_IMAGE = (
    "ghcr.io/zhiyuan-zhang0206/meiva-ci-runner@sha256:"
    "a17652b71203a57be18bd343938fc20d8227ccb40010aba8067a08b0e74b3029"
)
_LEGACY_PINS = (
    "linux-x86_64\n"
    "17.11.0 0dd7b72b6f335b8ecfb355fa24c5781e8a93edd09880bb77eb52ebbf29b3e96d\n"
    "0.8.7 acb5569b6a5bb71d5968205a4da15911ff3b2a15a57a2a9d765560882f53e3ea\n"
)
_LEGACY_ROOT = "/opt/meiva-vendored-pg/runtime"


def _contains_runtime(root: Path, *, system: str) -> bool:
    pg_dir = root / "pg" / rb.PG_VERSION
    extension = pg_dir / "share/postgresql/extension"
    suffix = rb.extension_library_suffix(system)
    return all(
        path.is_file()
        for path in (
            pg_dir / "bin/initdb",
            pg_dir / "bin/postgres",
            pg_dir / "bin/pg_ctl",
            pg_dir / f"lib/postgresql/vector{suffix}",
            extension / "vector.control",
            extension / rb.PGVECTOR_SQL,
        )
    )


def _image_pins_match(root: Path, image_ref: str, expected: str) -> bool:
    try:
        actual = (root / RUNTIME_PINS_FILENAME).read_text(encoding="ascii")
    except FileNotFoundError:
        return str(root) == _LEGACY_ROOT and image_ref == _LEGACY_IMAGE and expected == _LEGACY_PINS
    except (OSError, UnicodeError):
        return False
    return actual == expected


def prepare_runtime(
    *,
    home: Path,
    system: str,
    machine: str,
    image_root: Path | None = None,
    image_ref: str = "",
    open_url: UrlOpener | None = None,
    sleep: Callable[[float], None] = retry_sleep,
) -> Path:
    root = rb.runtime_root(home)
    if _contains_runtime(root, system=system):
        return root
    pg_key = rb.platform_key(system=system, machine=machine)
    vector_key = rb.pgvector_platform_key(system=system, machine=machine)
    expected = runtime_pin_manifest(
        vector_key,
        rb.PG_VERSION,
        rb.PG_ARTIFACTS[pg_key][1],
        rb.PGVECTOR_VERSION,
        rb.PGVECTOR_ARTIFACTS[vector_key][1],
    )
    if (
        image_root is not None
        and _image_pins_match(image_root, image_ref, expected)
        and _contains_runtime(image_root, system=system)
    ):
        return image_root
    _ = rb.ensure_pg_binaries(
        home=home, system=system, machine=machine, open_url=open_url, sleep=sleep
    )
    rb.ensure_pgvector(home=home, system=system, machine=machine, open_url=open_url)
    return root


@dataclass(frozen=True)
class _Options:
    github_env: Path
    github_output: Path | None
    image_runtime_root: str
    runner_image: str


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("github_env", type=Path)
    _ = parser.add_argument("--github-output", type=Path)
    _ = parser.add_argument("--image-runtime-root", default="")
    _ = parser.add_argument("--runner-image", default="")
    args = options(parser.parse_args(argv), _Options)
    home = meiva_home()
    system, machine = rb.artifact_system(Platform.current())
    root = prepare_runtime(
        home=home,
        system=system,
        machine=machine,
        image_root=Path(args.image_runtime_root) if args.image_runtime_root else None,
        image_ref=args.runner_image,
    )
    with args.github_env.open("a", encoding="utf-8") as env_file:
        _ = env_file.write(f"CI_VENDORED_RUNTIME_ROOT={root}\n")
    if args.github_output is not None and root == rb.runtime_root(home):
        with args.github_output.open("a", encoding="utf-8") as outputs:
            _ = outputs.write("cacheable=true\n")
    print(f"vendored Postgres runtime ready at {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
