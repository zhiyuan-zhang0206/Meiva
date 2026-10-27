# meiva:note
# A child process that builds the real service roster under the environment its parent chose
# and prints the probe addresses and launch commands the tests assert on as JSON. The roster
# reads process settings, so each environment needs a process of its own.

from __future__ import annotations

import json
import sys

from base.paths import meiva_home
from services.supervision.roster.composition import build_services


def main() -> None:
    specs = build_services(home=meiva_home())
    by_session = {spec.session: spec for spec in specs}
    report = {
        "sessions": sorted(by_session),
        "labeler_curl_url": by_session["labeler"].curl_url,
        "ops_curl_url": by_session["ops"].curl_url,
        "memory_search_tcp_port": by_session["memory-search"].tcp_port,
        "frontend_curl_url": by_session["frontend"].curl_url,
        "frontend_cmd": by_session["frontend"].cmd,
        "gateway_curl_url": by_session["gateway"].curl_url,
    }
    sys.stdout.write(json.dumps(report) + "\n")


if __name__ == "__main__":
    main()
