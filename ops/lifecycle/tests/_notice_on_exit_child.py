# meiva:note
# Child helper for test_termination_message that writes a marker file on exit or signal.

import pathlib
import signal
import sys
import time


def main() -> None:
    marker = pathlib.Path(sys.argv[1])

    def notify(*_args: object) -> None:
        marker.write_text("notice", encoding="utf-8")
        sys.exit(0)

    signal.signal(signal.SIGTERM, notify)
    signal.signal(signal.SIGHUP, notify)
    try:
        time.sleep(300)
    finally:
        marker.write_text("notice", encoding="utf-8")


if __name__ == "__main__":
    main()
