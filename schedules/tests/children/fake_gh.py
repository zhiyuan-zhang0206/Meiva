# meiva:note
# A program standing in for `gh api`, for the tests of the schedules that read GitHub.
#
# It answers only the calls the schedules make. A request carrying `--jq` is one of the CI
# exporter's run or pull-request listings and gets an empty list. A workflow-runs listing is
# answered from `FAKE_GH_WORKFLOWS`, a JSON object of workflow file to the conclusions of its
# runs, newest first; a file mapped to `"invalid"` gets a body that is not JSON. With
# `FAKE_GH_FAIL` set every call fails with a non-retryable HTTP error. Anything else exits 1.

from __future__ import annotations

import json
import os
import sys


def main(argv: list[str]) -> int:
    if os.environ.get("FAKE_GH_FAIL"):
        sys.stderr.write("HTTP 422: validation failed\n")
        return 1
    path = argv[argv.index("GET") + 1]
    if "--jq" in argv:
        sys.stdout.write("[]")
        return 0
    if "/actions/workflows/" in path:
        workflows = json.loads(os.environ["FAKE_GH_WORKFLOWS"])
        name = path.split("/actions/workflows/")[1].split("/")[0]
        conclusions = workflows[name]
        if conclusions == "invalid":
            sys.stdout.write("not json")
            return 0
        runs = [
            {
                "conclusion": conclusion,
                "html_url": f"https://example.test/{name}/{index}",
                "created_at": f"2026-09-0{index + 1}T00:00:00Z",
            }
            for index, conclusion in enumerate(conclusions)
        ]
        sys.stdout.write(json.dumps({"workflow_runs": runs}))
        return 0
    sys.stderr.write(f"unscripted gh call: {argv}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
