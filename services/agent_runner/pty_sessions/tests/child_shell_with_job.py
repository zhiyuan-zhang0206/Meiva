# meiva:note
# A stand-in shell with one background job: the job's pid is the first line it prints.
#
# A test that must show a cleanup leaves the shell's jobs alone starts this script as the session's
# shell and later reads the job's process state.
import signal
import subprocess
import sys
import time
from pathlib import Path


def _run_foreground_sync(root: Path) -> None:
    ready = root / "ready"
    interrupted = root / "interrupted"
    consumed = root / "consumed"
    release = root / "release"

    def interrupt(*_: object) -> None:
        interrupted.touch()
        temporary = consumed.with_suffix(".tmp")
        temporary.write_text(sys.stdin.readline(), encoding="utf-8")
        temporary.replace(consumed)
        while not release.exists():
            time.sleep(0.01)
        raise SystemExit(0)

    signal.signal(signal.SIGINT, interrupt)
    ready.touch()
    while True:
        time.sleep(1)


# meiva:note
# Entry point for the stand-in shell and its fixed test job modes; the child command is a literal repository path.
def main() -> None:
    if sys.argv[1:] == ["--job"]:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(300)
        return
    if sys.argv[1:2] == ["--foreground-sync"]:
        _run_foreground_sync(Path(sys.argv[2]))
        return
    job = (
        subprocess.Popen([sys.executable, str(Path(__file__)), "--job"])  # noqa: S603
        if sys.argv[1:] == ["--ignore-hangup"]
        else subprocess.Popen(["sleep", "300"])
    )
    sys.stdout.write(f"{job.pid}\n")
    sys.stdout.flush()
    time.sleep(300)


if __name__ == "__main__":
    main()
