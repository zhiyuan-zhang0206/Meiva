from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from base.host.permissions_helper.grants.probe import HelperChannel

GRANT_SERVICES = ("kTCCServiceScreenCapture", "kTCCServiceAccessibility")

DESCRIPTION = (
    "Ask the permissions helper to raise its Screen Recording and Accessibility dialogs, then"
    " record tccd's own verdict as evidence. tccd splits the two: the AUTHREQ_CTX line names"
    " the service and carries preflight=no for a real request, the AUTHREQ_RESULT line carries"
    " the verdict but only a msgID. The run therefore pairs them -- a preflight line can never"
    " pass as evidence of a request. Exit 0 = a verdict matched; 1 = the request ran but no"
    " verdict matched (check the window, or the dialog is still pending); 2 = setup problem"
    " (not macOS, helper unreachable, helper build without request_grants_v1). An Accessibility"
    " request may leave no tccd trace at all; Screen Recording is the service the acceptance run"
    " requires."
)

MSG_ID = re.compile(r"msgID=([0-9.]+)")


def _carries_exact_msg_id(line: str, message_id: str) -> bool:
    return re.search(rf"msgID={re.escape(message_id)}(?![0-9.])", line) is not None


def _requested_services(lines: list[str]) -> dict[str, set[str]]:
    services: dict[str, set[str]] = {}
    for line in lines:
        if "preflight=no" not in line:
            continue
        matched = {service for service in GRANT_SERVICES if service in line}
        if not matched:
            continue
        for message_id in MSG_ID.findall(line):
            services.setdefault(message_id, set()).update(matched)
    return services


def decision_evidence(output: str) -> tuple[list[str], set[str]]:
    lines = output.splitlines()
    services = _requested_services(lines)
    verdicts = {
        message_id
        for message_id in services
        if any(
            "AUTHREQ_RESULT" in line and _carries_exact_msg_id(line, message_id) for line in lines
        )
    }
    decided: set[str] = set()
    for message_id in verdicts:
        decided.update(services[message_id])
    rows = [
        line
        for line in lines
        if any(_carries_exact_msg_id(line, message_id) for message_id in verdicts)
    ]
    return list(dict.fromkeys(rows)), decided


def tccd_log_argv(start: datetime, window: int) -> list[str]:
    local = start.astimezone()
    end = local + timedelta(seconds=window)
    fmt = "%Y-%m-%d %H:%M:%S"
    return [
        "log",
        "show",
        "--start",
        local.strftime(fmt),
        "--end",
        end.strftime(fmt),
        "--predicate",
        'process == "tccd"',
        "--style",
        "compact",
    ]


# meiva:note
# `tccd_log_argv` builds a fixed `log show` argument list from formatted timestamps.
def tccd_log(start: datetime, window: int) -> str:
    done = subprocess.run(  # noqa: S603
        tccd_log_argv(start, window),
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout or ""


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--out", help="evidence directory (default: $MEIVA_HOME/tcc-evidence)")
    parser.add_argument("--window", type=int, default=120, help="seconds after the request")
    parser.add_argument(
        "--require",
        choices=GRANT_SERVICES,
        help="also require a decision line for this service (Screen Recording acceptance)",
    )
    return parser.parse_args(argv)


def _request_helper_grants(
    client: HelperChannel, *, platform: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | int:
    if platform != "darwin":
        print("this evidence run needs macOS", file=sys.stderr)
        return 2
    try:
        before: dict[str, Any] = dict(client.ping())
    except Exception as exc:
        print(f"the permissions helper did not answer: {exc}", file=sys.stderr)
        return 2
    if before.get("request_grants_v1") is not True:
        print(
            "the running helper predates request_grants_v1; update it before recording evidence",
            file=sys.stderr,
        )
        return 2
    requested = dict(client.request_grants())
    after = dict(client.ping())
    return before, requested, after


def _write_evidence(
    args: argparse.Namespace,
    *,
    home: Path,
    started: datetime,
    before: dict[str, Any],
    requested: dict[str, Any],
    after: dict[str, Any],
    log_output: str,
) -> int:
    lines, decided = decision_evidence(log_output)
    out = Path(args.out) if args.out else home / "tcc-evidence"
    target = out / f"request-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "started_at": started.isoformat(),
                "helper_pid": before.get("pid"),
                "before": before,
                "requested": requested,
                "after": after,
                "required_service": args.require,
                "decided_services": sorted(decided),
                "tccd_lines": lines,
            },
            indent=2,
        )
    )
    print(f"evidence: {target}")
    for line in lines:
        print(f"  tccd: {line}")
    if args.require and args.require not in decided:
        print(f"tccd recorded no verdict for {args.require}", file=sys.stderr)
        return 1
    if not lines:
        print(
            "no tccd verdict in the window; the dialog may still be pending -- widen --window"
            " and run again",
            file=sys.stderr,
        )
        return 1
    return 0


def run_evidence(
    argv: list[str],
    *,
    client: HelperChannel,
    platform: str,
    now: datetime,
    log_output: str,
    home: Path,
) -> int:
    args = _parse_args(argv)
    outcome = _request_helper_grants(client, platform=platform)
    if isinstance(outcome, int):
        return outcome
    before, requested, after = outcome
    return _write_evidence(
        args,
        home=home,
        started=now,
        before=before,
        requested=requested,
        after=after,
        log_output=log_output,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if sys.platform != "darwin":
        print("this evidence run needs macOS", file=sys.stderr)
        return 2
    from base.paths import meiva_home
    from services.desktop.permissions_helper.onboarding import helper_client

    started = datetime.now(UTC)
    outcome = _request_helper_grants(helper_client(), platform=sys.platform)
    if isinstance(outcome, int):
        return outcome
    before, requested, after = outcome
    return _write_evidence(
        args,
        home=meiva_home(),
        started=started,
        before=before,
        requested=requested,
        after=after,
        log_output=tccd_log(started, args.window),
    )


if __name__ == "__main__":
    raise SystemExit(main())
