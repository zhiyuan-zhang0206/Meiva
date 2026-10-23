# meiva:link ops/docs/env-audit.meiva.okf.md
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import psutil

from base.paths.home import resolve_meiva_home
from ops.env_audit import record_env_write


def main() -> None:
    assert sys.argv[1:] == ["cluster-secret-value"]
    process = psutil.Process()
    observed_argv = process.cmdline()
    assert "cluster-secret-value" in observed_argv
    observed_name = str(process.name())
    home = resolve_meiva_home()
    env_path = home / ".env"
    env_path.write_text("MEIVA_MODEL=test-model\n")
    record_env_write(env_path, {"MEIVA_MODEL"}, set(), site="test")
    audit_path = home / ".env.audit.jsonl"
    raw = audit_path.read_text()
    record = json.loads(raw)
    assert record["pid"] == os.getpid()
    assert record["process"] == observed_name
    assert record["cmdline"] == f"{Path(observed_argv[0]).name} [arguments redacted]"[:200]
    assert "cluster-secret-value" not in raw
    assert audit_path.stat().st_mode & 0o777 == 0o600
    assert (home / ".env.audit.armed").stat().st_mode & 0o777 == 0o600


if __name__ == "__main__":
    main()
