import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) == 1:
        return
    token = sys.stdin.buffer.readline()
    if token:
        Path(sys.argv[1]).write_bytes(token)


if __name__ == "__main__":
    main()
