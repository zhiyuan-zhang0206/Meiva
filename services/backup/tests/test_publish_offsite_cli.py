# meiva:note
# The standalone `--publish-offsite` entry: success and failure both leave evidence on stderr.
#
# Each case runs `python -m services.backup.dump` in a fresh interpreter, configured through
# the environment the way an operator configures it, against the loopback OSS server of
# `services/backup/artifact/tests/oss_fake.py`.

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from services.backup.artifact.names import REMOTE_ROOT
from services.backup.artifact.tests.oss_fake import BUCKET, FakeOss, fake_oss

_REPO_ROOT = Path(__file__).resolve().parents[3]
_NAME = "meiva-20260916T030000Z.dump.enc"


def _artifact(tmp_path: Path, data: bytes = b"encrypted artifact") -> Path:
    path = tmp_path / _NAME
    path.write_bytes(data)
    return path


# meiva:note
# Run the entry in a fresh interpreter over a home of its own.
#
# A fresh interpreter is deliberate: pytest's own root handler would mask the standalone
# logging behavior the entry point configures. `server` gives the home an OSS destination
# in its `.env`, the way an operator configures one; None leaves the destination unset.
def _entry(tmp_path: Path, server: FakeOss | None, *args: str) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    home.mkdir()
    if server is not None:
        credentials = tmp_path / "oss.json"
        credentials.write_text('{"access_key_id": "id", "access_key_secret": "secret"}')
        (home / ".env").write_text(
            f"MEIVA_BACKUP_OFFSITE_ENDPOINT={server.endpoint}\n"
            f"MEIVA_BACKUP_OFFSITE_BUCKET={BUCKET}\n"
            f"MEIVA_BACKUP_OFFSITE_CREDENTIALS_FILE={credentials}\n"
        )
    env = {key: value for key, value in os.environ.items() if not key.startswith("MEIVA_")}
    env.update(MEIVA_HOME=str(home), MEIVA_CONFIG_FETCH="skip", MEIVA_PROCESS_PROFILE="gateway")
    return subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "services.backup.dump",
            "--publish-offsite",
            str(tmp_path / _NAME),
            *args,
        ],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_standalone_success_is_visible(tmp_path: Path) -> None:
    """A fully silent success was once read as a dead upload (misdiagnosed 2026-09-16)."""
    _artifact(tmp_path)

    with fake_oss() as server:
        proc = _entry(tmp_path, server, "--offsite-root", "meiva-pitr-scratch/smoke")

    assert proc.returncode == 0, proc.stderr
    assert "[backup] off-site published" in proc.stderr
    assert f"meiva-pitr-scratch/smoke/{_NAME}" in proc.stderr
    assert server.files[f"meiva-pitr-scratch/smoke/{_NAME}"]["data"] == b"encrypted artifact"


def test_standalone_unconfigured_still_says_so_and_exits_zero(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)

    proc = _entry(tmp_path, None)

    assert proc.returncode == 0, proc.stderr
    assert "[backup] off-site publish skipped" in proc.stderr
    assert artifact.read_bytes() == b"encrypted artifact"


def test_standalone_publish_failure_keeps_exit_and_artifact(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)

    with fake_oss() as server:
        server.fail_init = (500, "InternalError")
        proc = _entry(tmp_path, server)

    assert proc.returncode == 0, proc.stderr
    assert f"[backup] off-site publish of {REMOTE_ROOT}/{_NAME} failed" in proc.stderr
    assert artifact.read_bytes() == b"encrypted artifact"
