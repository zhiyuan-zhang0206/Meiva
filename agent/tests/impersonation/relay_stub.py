import json
import os
import sys
import time
from pathlib import Path


def main() -> None:
    directory, mode, *command = sys.argv[1:]
    marker = Path(directory)
    token = sys.stdin.buffer.readline()
    (marker / "argv.json").write_text(json.dumps(command), encoding="utf-8")
    (marker / "token").write_bytes(token)
    (marker / "pid").write_text(str(os.getpid()), encoding="utf-8")
    (marker / "env.json").write_text(json.dumps(dict(os.environ)), encoding="utf-8")
    kind, _, argument = mode.partition(":")
    if kind == "exit":
        sys.exit(int(argument))
    if kind == "serve":
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            time.sleep(0.05)


if __name__ == "__main__":
    main()
