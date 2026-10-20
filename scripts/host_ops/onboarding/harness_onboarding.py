#!/usr/bin/env python
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.host_ops.onboarding import catalog, hostfs, transport
from scripts.host_ops.onboarding.transport import Host

DEFAULT_CHECKOUT = "~/.meiva/source"
DEFAULT_INTERPRETER = ".venv/bin/python"
BACKUP_ROOT = "~/.meiva/backups/onboarding-{stamp}"
STATE_LOG = "~/.meiva/onboarding/install-log.jsonl"

_VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?")
_SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")

PROBE_SOURCE = """\
import json

from base.agents.birth.harness import probe_local_harnesses

rows = [
    {
        "harness": probe.harness.value,
        "status": probe.status,
        "reason": probe.reason,
        "binary": probe.binary,
        "on_path": probe.on_path,
    }
    for probe in probe_local_harnesses()
]
print("__PROBE__" + json.dumps(rows))
"""

RUNTIME_TO_CLI = {spec.runtime: spec.cli for spec in catalog.CATALOG.values() if spec.runtime}


@dataclass(frozen=True)
class Evidence:
    host: str
    harness: str
    status: str
    reason: str
    binary: str | None
    on_path: bool
    runs: str = ""
    login: str = ""

    @property
    def disagreement(self) -> str:
        if self.status == "available" and self.login.startswith("out"):
            return "probe says available, the CLI says it is signed out"
        if self.status == "unavailable" and self.login.startswith("in"):
            return "probe says unavailable, the CLI says it is signed in"
        if self.status == "available" and self.runs.startswith("FAILED"):
            return "probe says available, the binary does not run"
        return ""


def redact(text: str) -> str:
    return _SECRET_PATTERN.sub("sk-***", text)


def parse_version(text: str) -> str:
    match = _VERSION_PATTERN.search(text)
    return match.group(0) if match else ""


def collect_probe(host: Host, checkout: str, interpreter: str) -> list[dict[str, object]]:
    result = host.run(f"cd {checkout} && {interpreter} -", stdin=PROBE_SOURCE.encode("utf-8"))
    if not result.ok:
        raise RuntimeError(
            f"{host}: the harness probe failed at {checkout} "
            f"(exit {result.exit_code}): {redact(result.stderr.strip())[-400:]}"
        )
    for line in result.stdout.splitlines():
        if line.startswith("__PROBE__"):
            rows = json.loads(line[len("__PROBE__") :])
            if isinstance(rows, list):
                return rows
    raise RuntimeError(f"{host}: the harness probe printed no rows")


def collect_version(host: Host, binary: str | None) -> str:
    if not binary:
        return ""
    result = host.run(f"{binary} --version 2>&1 | head -3", timeout=60)
    if result.exit_code != 0:
        first = (result.stdout or result.stderr).strip().splitlines()
        return "FAILED: " + (redact(first[0]) if first else f"exit {result.exit_code}")[:60]
    return parse_version(result.stdout) or result.stdout.strip().splitlines()[0][:40]


# meiva:note
# The Claude check unsets ANTHROPIC_API_KEY first: an exported key makes the CLI
# report api_key as its auth method even when a subscription login exists, so the
# answer would describe the environment instead of the stored login.
# meiva:link scripts/host_ops/docs/harness-onboarding.meiva.okf.md
def collect_login(host: Host, harness: str, binary: str | None) -> str:
    cli = RUNTIME_TO_CLI.get(harness, harness)
    if not binary:
        return ""
    if cli == "claude":
        result = host.run(f"unset ANTHROPIC_API_KEY; {binary} auth status", timeout=60)
        try:
            payload = json.loads(result.stdout[result.stdout.find("{") :])
        except ValueError:
            return "unknown"
        if payload.get("loggedIn") is True:
            return f"in ({payload.get('authMethod', '?')})"
        return "out"
    if cli == "codex":
        result = host.run(f"{binary} login status", timeout=60)
        text = redact(result.stdout + result.stderr).lower()
        if "not logged in" in text or "logged out" in text:
            return "out"
        if result.ok and "logged in" in text:
            return "in"
        return "unknown"
    return ""


def gather(host: Host, checkout: str, interpreter: str, *, verify: bool) -> list[Evidence]:
    evidence: list[Evidence] = []
    for row in collect_probe(host, checkout, interpreter):
        harness = str(row.get("harness", ""))
        binary = row.get("binary")
        entry = Evidence(
            host=host.name,
            harness=harness,
            status=str(row.get("status", "")),
            reason=str(row.get("reason", "")),
            binary=str(binary) if binary else None,
            on_path=row.get("on_path") is True,
        )
        if verify:
            entry = replace(
                entry,
                runs=collect_version(host, entry.binary),
                login=collect_login(host, harness, entry.binary),
            )
        evidence.append(entry)
    return evidence


def first_file(payload: dict[str, object]) -> dict[str, object]:
    files = payload.get("files")
    if isinstance(files, list) and files and isinstance(files[0], dict):
        return cast("dict[str, object]", files[0])
    return {}


def shorten(text: str) -> str:
    home = str(Path.home())
    return text.replace(home, "~") if text.startswith(home) else text


def format_cell(value: object, width: int) -> str:
    text = str(value)
    if len(text) > width:
        text = text[: width - 1] + "\u2026"
    return text.ljust(width + 1)


def render(rows: list[Evidence], *, verify: bool) -> str:
    columns = [("host", 14), ("harness", 12), ("probe", 12), ("binary", 46)]
    if verify:
        columns += [("runs", 22), ("login", 16)]
    lines = ["".join(name.ljust(width + 1) for name, width in columns).rstrip()]
    for row in rows:
        cells = {
            "host": row.host,
            "harness": row.harness,
            "probe": row.status,
            "binary": shorten(row.binary) if row.binary else "-",
            "runs": row.runs or "-",
            "login": row.login or "-",
        }
        line = "".join(format_cell(cells[name], width) for name, width in columns)
        if row.disagreement:
            line = line.rstrip() + "  <-- " + row.disagreement
        lines.append(line.rstrip())
    return "\n".join(lines)


def cmd_doctor(args: argparse.Namespace) -> int:
    rows: list[Evidence] = []
    failed = False
    for host in transport.parse_hosts(args.hosts):
        try:
            rows.extend(gather(host, args.checkout, args.interpreter, verify=args.verify))
        except (RuntimeError, ValueError) as error:
            failed = True
            print(f"error: {error}", file=sys.stderr)
    if not rows:
        return 1
    if args.json:
        print(json.dumps([row.__dict__ for row in rows], indent=2))
    else:
        print(render(rows, verify=args.verify))
        disagreements = [row for row in rows if row.disagreement]
        if disagreements:
            print(
                f"\n{len(disagreements)} row(s) disagree with themselves; "
                "treat the probe column as unverified there."
            )
    return 1 if failed else 0


def parse_pins(values: list[str] | None) -> dict[str, str]:
    pins: dict[str, str] = {}
    for item in values or []:
        if "=" not in item:
            raise SystemExit(f"--pin expects harness=version, got {item!r}")
        name, version = item.split("=", 1)
        pins[name.strip()] = version.strip()
    return pins


def installed_version(host: Host, harness: catalog.HarnessSpec) -> str:
    output = host.run(f"{harness.binary} --version 2>&1 | head -3", timeout=60).stdout
    return parse_version(output)


def require_prerequisites(host: Host, harness: catalog.HarnessSpec) -> str:
    needed = "npm" if harness.needs_node else "curl"
    if host.run(f"command -v {needed}").ok:
        return ""
    which = "node and npm" if harness.needs_node else "curl"
    return f"{host}: {needed} is missing, so {harness.cli} cannot be installed (needs {which})"


def npm_global_root_writable(host: Host) -> bool:
    # meiva:note
    # A distribution node on Linux installs its global packages under /usr/lib/node_modules,
    # which needs root. Ask npm where it would write, then ask the shell whether this user can
    # write there: a user-local prefix beats an install that dies on mkdir.
    # meiva:link scripts/host_ops/docs/harness-onboarding.meiva.okf.md
    root = host.run("npm root -g", timeout=60)
    directory = root.stdout.strip()
    return bool(directory) and host.run(f'test -w "{directory}"', timeout=60).ok


def agent_sees(host: Host, binary: str) -> str:
    # meiva:note
    # The path a service or ssh command resolves for `binary`, empty when it resolves none.
    # The probe preamble adds ~/.local/bin, which a service never has, so this asks the
    # agent's own search path instead.
    # meiva:link scripts/host_ops/docs/harness-onboarding.meiva.okf.md
    found = host.run(f"command -v {binary}", timeout=60, agent_path=True)
    return found.stdout.strip() if found.ok else ""


def reach_agent_path(host: Host, harness: catalog.HarnessSpec) -> tuple[str, str]:
    # meiva:note
    # A user-local install is only useful where a service looks: link it into /usr/local/bin
    # when that is writable, otherwise return the exact command an operator must run.
    # meiva:link scripts/host_ops/docs/harness-onboarding.meiva.okf.md
    if agent_sees(host, harness.cli):
        return "", ""
    link = f"/usr/local/bin/{harness.cli}"
    if host.run(f"test -w {link.rsplit('/', 1)[0]}", timeout=60).ok:
        linked = host.run(f'ln -sf "$HOME/.local/bin/{harness.cli}" "{link}"', timeout=60)
        if linked.ok and agent_sees(host, harness.cli):
            return f"linked {link} -> ~/.local/bin/{harness.cli}", ""
    return "", (
        f"{harness.cli} is installed under ~/.local/bin, which neither the agent search path "
        f"({transport.AGENT_PATH}) nor a writable /usr/local/bin covers; link it as root: "
        f"sudo ln -sf ~/.local/bin/{harness.cli} {link}"
    )


def hostfs_source() -> str:
    return Path(hostfs.__file__).read_text(encoding="utf-8")


def record_install(
    host: Host, harness: catalog.HarnessSpec, requested: str, installed: str
) -> None:
    host.python(
        hostfs_source(),
        {
            "op": "append",
            "path": STATE_LOG,
            "record": {
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
                "harness": harness.cli,
                "requested": requested,
                "installed": installed,
                "method": f"{harness.installer.kind}:{harness.installer.target}",
            },
        },
    )


def install_prefix(host: Host, harness: catalog.HarnessSpec) -> str:
    # meiva:note
    # The `--prefix` an install needs when the global npm root is out of reach, empty when
    # npm can write there or the installer is not npm's.
    # meiva:link scripts/host_ops/docs/harness-onboarding.meiva.okf.md
    if harness.installer.kind != "npm" or npm_global_root_writable(host):
        return ""
    return '"$HOME/.local"'


def install_on(host: Host, harness: catalog.HarnessSpec, wanted: str, prefix: str) -> bool:
    # meiva:note
    # One install on one host; True when a service on that host can then run the binary, so
    # a user-local install that no agent search path reaches is a failure, not an "ok".
    # meiva:link scripts/host_ops/docs/harness-onboarding.meiva.okf.md
    command = catalog.install_command(harness, wanted, prefix=prefix)
    print(
        f"install {host}: {harness.cli} {wanted}"
        + (" (user-local: the global npm root is not writable)" if prefix else "")
    )
    result = host.run(command, timeout=900)
    if not result.ok:
        print(
            f"FAILED {host}: {harness.cli} install exited {result.exit_code}\n"
            f"  {redact(result.stderr.strip())[-600:]}",
            file=sys.stderr,
        )
        return False
    installed = installed_version(host, harness)
    record_install(host, harness, wanted, installed)
    flag = "" if installed == wanted else f"  (asked {wanted}, got {installed or 'nothing'})"
    if prefix:
        note, problem = reach_agent_path(host, harness)
        if problem:
            print(f"FAILED {host}: {problem}", file=sys.stderr)
            return False
        if note:
            print(f"  note {host}: {note}")
    print(f"  ok   {host}: {harness.cli} {installed or 'unknown'}{flag}")
    return True


def cmd_install(args: argparse.Namespace) -> int:
    hosts = transport.parse_hosts(args.hosts)
    pins = parse_pins(args.pin)
    failures = 0
    for name in args.harness:
        harness = catalog.spec(name)
        wanted = pins.get(name, harness.pinned_version)
        for host in hosts:
            problem = require_prerequisites(host, harness)
            if problem:
                failures += 1
                print(f"skip  {problem}", file=sys.stderr)
                continue
            prefix = install_prefix(host, harness)
            if args.dry_run:
                command = catalog.install_command(harness, wanted, prefix=prefix)
                print(f"[dry-run] {host}: {command}")
                continue
            if not install_on(host, harness, wanted, prefix):
                failures += 1
    return 1 if failures else 0


def copy_file(source: Host, target: Host, raw_path: str, backup_dir: str, *, dry_run: bool) -> str:
    stat = source.python(hostfs_source(), {"op": "stat", "paths": [raw_path]}).json_payload()
    described = first_file(stat)
    if not described.get("exists"):
        return f"skip {raw_path} (absent on {source})"
    if described.get("is_dir"):
        return f"skip {raw_path} (directory)"
    if dry_run:
        return f"[dry-run] copy {raw_path} -> {target}"
    payload = source.python(hostfs_source(), {"op": "read", "path": raw_path}).json_payload()
    if "error" in payload:
        return f"FAILED read {raw_path} on {source}: {payload['error']}"
    target.python(hostfs_source(), {"op": "backup", "path": raw_path, "dir": backup_dir})
    return _write_payload(target, raw_path, payload)


def _write_payload(target: Host, raw_path: str, payload: dict[str, object]) -> str:
    written = target.python(
        hostfs_source(),
        {
            "op": "write",
            "path": raw_path,
            "b64": payload["b64"],
            "mode": payload.get("mode", "600"),
        },
    ).json_payload()
    if "error" in written:
        return f"FAILED write {raw_path} on {target}: {written['error']}"
    return f"copy {raw_path} -> {target}"


def cmd_sync(args: argparse.Namespace) -> int:
    source = Host(args.source)
    targets = transport.parse_hosts(args.to)
    stamp = datetime.now().astimezone().strftime("%Y%m%d")
    backup_dir = BACKUP_ROOT.format(stamp=stamp)
    failures = 0
    for name in args.harness:
        harness = catalog.spec(name)
        source_version = installed_version(source, harness)
        if not source_version:
            failures += 1
            print(f"FAILED {source} does not have a working {harness.cli}", file=sys.stderr)
            continue
        print(f"\n{harness.cli} {source_version} from {source}")
        for target in targets:
            if target.name != source.name:
                failures += sync_target(source, source_version, target, harness, backup_dir, args)
    return 1 if failures else 0


def sync_target(
    source: Host,
    source_version: str,
    target: Host,
    harness: catalog.HarnessSpec,
    backup_dir: str,
    args: argparse.Namespace,
) -> int:
    if not match_version(target, harness, source_version, args):
        return 1
    for path in harness.config:
        print(f"  {copy_file(source, target, path, backup_dir, dry_run=args.dry_run)}")
    for store in harness.stores:
        print("  " + sync_store(source, target, harness, store, backup_dir, args))
    return 0


def match_version(
    target: Host, harness: catalog.HarnessSpec, source_version: str, args: argparse.Namespace
) -> bool:
    current = installed_version(target, harness)
    if current == source_version:
        return True
    if args.skip_install:
        print(f"  warn {target}: runs {current or 'nothing'}, source has {source_version}")
        return True
    if not harness.installer.pins:
        print(
            f"  FAILED {target}: {harness.cli} {harness.installer.target} cannot pin "
            f"{source_version}; install the same vendor version by hand before syncing",
            file=sys.stderr,
        )
        return False
    if version_key(current) > version_key(source_version):
        print(
            f"  warn {target}: runs {current}, newer than the source's {source_version}; "
            "keeping it, so sync from the newer machine instead"
        )
        return True
    print(f"  install {target}: {harness.cli} {source_version}")
    if args.dry_run:
        return True
    result = target.run(catalog.install_command(harness, source_version), timeout=900)
    if result.ok:
        return True
    print(f"  FAILED {target}: {redact(result.stderr.strip())[-400:]}", file=sys.stderr)
    return False


def version_key(text: str) -> tuple[int, ...]:
    keys = []
    for chunk in text.split("."):
        digits = "".join(character for character in chunk if character.isdigit())
        keys.append(int(digits) if digits else 0)
    return tuple(keys)


def sync_store(
    source: Host,
    target: Host,
    harness: catalog.HarnessSpec,
    store: catalog.Store,
    backup_dir: str,
    args: argparse.Namespace,
) -> str:
    stat = source.python(hostfs_source(), {"op": "stat", "paths": [store.path]}).json_payload()
    described = first_file(stat)
    if not described.get("exists"):
        return f"skip {store.path} (absent on {source})"
    if args.dry_run:
        return f"[dry-run] credentials {store.path} -> {store.destination} ({store.rotation})"
    payload = source.python(hostfs_source(), {"op": "read", "path": store.path}).json_payload()
    if "error" in payload:
        return f"FAILED read {store.path}: {payload['error']}"
    content = base64.b64decode(str(payload["b64"]))
    rotation = catalog.credential_risk(harness, store.path, content)
    if rotation != "static" and not args.accept_credential_risk:
        return (
            f"WITHHELD {store.path}: rotation is '{rotation}' — {store.why} "
            "Sign in on the target once, or pass --accept-credential-risk."
        )
    target.python(hostfs_source(), {"op": "backup", "path": store.destination, "dir": backup_dir})
    written = target.python(
        hostfs_source(),
        {
            "op": "write",
            "path": store.destination,
            "b64": payload["b64"],
            "mode": payload.get("mode", "600"),
        },
    ).json_payload()
    if "error" in written:
        return f"FAILED write {store.destination}: {written['error']}"
    return f"credentials {store.path} -> {store.destination} ({rotation}, backup in {backup_dir})"


# meiva:note
# `doctor` runs the platform probe and, with --verify, the two checks the probe
# deliberately does not make (does the binary execute, does the CLI say it is
# signed in). The columns stay separate because a probe that must never start a
# process cannot also be the authority on a live login; a row whose sources
# disagree is printed with the disagreement rather than resolved silently.
# meiva:link scripts/host_ops/docs/harness-onboarding.meiva.okf.md
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness_onboarding",
        description="Install, verify and sync the external coding harnesses across machines.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    doctor = subcommands.add_parser("doctor", help="print the per-machine harness matrix")
    doctor.add_argument("--hosts", nargs="*", help="ssh names; 'local' means this machine")
    doctor.add_argument("--checkout", default=DEFAULT_CHECKOUT, help="Meiva checkout on each host")
    doctor.add_argument(
        "--interpreter", default=DEFAULT_INTERPRETER, help="interpreter inside that checkout"
    )
    doctor.add_argument(
        "--verify", action="store_true", help="also run the binary and ask the CLI about its login"
    )
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(handler=cmd_doctor)

    install = subcommands.add_parser("install", help="install a harness from its vendor")
    install.add_argument("harness", nargs="+", choices=list(catalog.CLI_NAMES))
    install.add_argument("--hosts", nargs="*")
    install.add_argument("--pin", nargs="*", metavar="HARNESS=VERSION")
    install.add_argument("--dry-run", action="store_true")
    install.set_defaults(handler=cmd_install)

    sync = subcommands.add_parser(
        "sync", help="copy a harness's CLI, config and login from a configured machine"
    )
    sync.add_argument("harness", nargs="+", choices=list(catalog.CLI_NAMES))
    sync.add_argument("--from", dest="source", required=True, metavar="HOST")
    sync.add_argument("--to", nargs="+", required=True, metavar="HOST")
    sync.add_argument("--skip-install", action="store_true", help="never install, report a gap")
    sync.add_argument(
        "--accept-credential-risk",
        action="store_true",
        help="copy login stores whose rotation can sign the source machine out",
    )
    sync.add_argument("--dry-run", action="store_true")
    sync.set_defaults(handler=cmd_sync)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = cast("Callable[[argparse.Namespace], int]", args.handler)
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
