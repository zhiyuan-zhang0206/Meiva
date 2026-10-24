# meiva:note
# Fresh-interpreter probes for the scheduler daemon's OSS SDK import boundary.
#
# Usage: `child_import_probe.py daemon <repo> <roots...>` records, at the first
# import of the SDK, which repo frames asked for it while the daemon is imported.
# `child_import_probe.py worker <repo> <credentials> <work> <roots...>` imports the
# backup modules, then publishes an empty artifact so the real off-site publish opens
# the bucket with the SDK. Both print one JSON report line on stdout.

import importlib.abc
import json
import logging
import sys
import traceback
from pathlib import Path


def _daemon_probe(repo: str, roots: tuple[str, ...]) -> None:
    first_importer: list[str] = []

    class _Spy(importlib.abc.MetaPathFinder):
        def find_spec(self, name: str, path: object = None, target: object = None) -> None:
            if name.split(".", maxsplit=1)[0] in roots and not first_importer:
                first_importer.extend(
                    f"{frame.filename[len(repo) + 1 :]}:{frame.lineno}"
                    for frame in traceback.extract_stack()
                    if frame.filename.startswith(repo) and "/.venv/" not in frame.filename
                )

    sys.meta_path.insert(0, _Spy())
    import services.backup.scheduler.daemon  # noqa: F401  # pyright: ignore[reportUnusedImport]

    sys.stdout.write(
        json.dumps(
            {
                "loaded": sorted(m for m in sys.modules if m.split(".")[0] in roots),
                "first_importer": first_importer,
            }
        )
        + "\n"
    )


def _worker_probe(credentials_path: str, work_path: str, roots: tuple[str, ...]) -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)

    import services.backup.dump as dump_module
    import services.backup.scheduler.worker as worker_module

    before = sorted(m for m in sys.modules if m.split(".")[0] in roots)
    credentials = Path(credentials_path)
    credentials.write_text(json.dumps({"access_key_id": "id", "access_key_secret": "secret"}))
    work = Path(work_path)
    work.mkdir()
    artifact = work / "meiva-20261002T030000Z.dump.enc"
    artifact.write_bytes(b"")

    from services.backup.artifact import offsite
    from services.backup.artifact.offsite_plan import OssTarget

    published = offsite.publish(artifact, OssTarget("https://oss.invalid", "backups", credentials))
    sys.stdout.write(
        json.dumps(
            {
                "before": before,
                "imported": [dump_module.__name__, worker_module.__name__],
                "published": published,
                "bucket_class": sys.modules["oss2"].Bucket.__name__,
            }
        )
        + "\n"
    )


if __name__ == "__main__":
    if sys.argv[1] == "daemon":
        _daemon_probe(sys.argv[2], tuple(sys.argv[3:]))
    elif sys.argv[1] == "worker":
        _worker_probe(sys.argv[3], sys.argv[4], tuple(sys.argv[5:]))
    else:
        raise SystemExit(f"unknown probe {sys.argv[1]!r}")
