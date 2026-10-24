# meiva:note
# A stand-in shell with one background job: the job's pid is the first line it prints.
#
# A test that must show a cleanup leaves the shell's jobs alone starts this script as the session's
# shell and later reads the job's process state.
import subprocess
import sys
import time


def main() -> None:
    job = subprocess.Popen(["sleep", "300"])
    sys.stdout.write(f"{job.pid}\n")
    sys.stdout.flush()
    time.sleep(300)


if __name__ == "__main__":
    main()
