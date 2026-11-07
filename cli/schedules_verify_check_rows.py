# meiva:note
# Offline stored-schedule check entry shipped to the gateway's throwaway NEW worktree: invokes
# `meiva schedules verify --rows-file <rows> --no-notify` under the worktree's own `cli.main`.
import subprocess
import sys

if __name__ == "__main__":
    rows = sys.argv[1]
    command = [
        sys.executable,
        "-m",
        "cli.main",
        "schedules",
        "verify",
        "--rows-file",
        rows,
        "--no-notify",
    ]
    sys.exit(
        subprocess.run(command, check=False).returncode  # noqa: S603
    )
