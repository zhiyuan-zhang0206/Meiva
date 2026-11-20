# meiva:note
# A program standing in for `gh api`, for the tests of the schedules that read GitHub.
#
# It answers only the calls the schedules make. A request carrying `--jq` is one of the CI
# exporter's run or pull-request listings and gets an empty list. With `FAKE_GH_FAIL` set every
# call fails with a non-retryable HTTP error. Anything else exits 1.

from __future__ import annotations

import os
import sys


def main(argv: list[str]) -> int:
    if os.environ.get("FAKE_GH_FAIL"):
        sys.stderr.write("HTTP 422: validation failed\n")
        return 1
    if "--jq" in argv:
        sys.stdout.write("[]")
        return 0
    sys.stderr.write(f"unscripted gh call: {argv}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
