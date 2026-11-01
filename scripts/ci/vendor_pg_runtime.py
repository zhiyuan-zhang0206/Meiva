#!/usr/bin/env python
"""Put the vendored Postgres (pinned zonky + injected pgvector) where CI test sessions link it.

Production runs the server from this exact tree (`base.cluster.dataplane.runtime_binaries`);
a CI job that tests against an apt build instead is testing a different Postgres. Each
pytest session redirects `MEIVA_HOME` to a temp home, so the tree is built once per job under
a stable home and exported as `CI_VENDORED_RUNTIME_ROOT`; `tests/fixtures/env_bootstrap.py`
symlinks it into every session's home.

Usage: `vendor_pg_runtime.py <github-env-file>`, with `MEIVA_HOME` naming the stable home to
build under. Idempotent: a restored cache makes both ensures no-ops.
"""

from __future__ import annotations

import sys
from pathlib import Path

from base.cluster.dataplane import runtime_binaries as rb
from base.native_process.os_platform import Platform
from base.paths import meiva_home


def main(github_env: Path) -> int:
    home = meiva_home()
    system, machine = rb.artifact_system(Platform.current())
    rb.ensure_pg_binaries(home=home, system=system, machine=machine)
    rb.ensure_pgvector(home=home, system=system, machine=machine)
    root = rb.runtime_root(home)
    with github_env.open("a", encoding="utf-8") as env_file:
        env_file.write(f"CI_VENDORED_RUNTIME_ROOT={root}\n")
    print(f"vendored Postgres runtime ready at {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
