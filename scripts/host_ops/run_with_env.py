# meiva:note
# Run one command with this home's database and cache URLs in its environment (task #5866).
#
# A pty session no longer carries `MEIVA_DB_URL` / `MEIVA_REDIS_URL` (they hold the unit's database
# password and the cache password, the highest-value pair of the batch), so `psql "$MEIVA_DB_URL"`
# from a plain shell stops working. This entry restores that channel for exactly one command: it
# reads each value fresh, pins it into the CHILD environment and execs. Nothing is cached, nothing
# is written, and neither value reaches argv or this script's stdout.
#
# Two sources, deliberately different:
# - the database URL comes from the home's installed capability (`consume_unit(home).dsn`), the
#   same admission the boot path uses: a login, with the unit role and password assembled from
#   `db-authority/unit.json`;
# - the cache URL comes from the home's `.env`: it is cluster configuration, not part of the unit
#   capability, and no capability field carries it. Reading the file line is the same route
#   `schedules/jobs/self_evolution_weekly.py` takes for the cluster secret.
# A future reader must not unify them: one is an admission, the other a file value, and their
# failure modes differ (a refused capability versus a home whose `.env` has no such line).
#
# Like `run_with_token.py`, this works only inside the home's admitted runtime: `consume_unit`
# refuses any other checkout, which is the point.
from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from base.host.db_authority.model import AuthorityRefusedError
from base.host.db_authority.unit import consume_unit
from base.paths.home import resolve_meiva_home

# meiva:note
# Environment key names, not credential values.
DB_ENV = "MEIVA_DB_URL"
REDIS_ENV = "MEIVA_REDIS_URL"

PROVIDERS = ("db", "redis")


# meiva:note
# A provider that cannot supply its value: the caller must see a non-zero exit and a reason, never
# an empty URL that turns into a confusing tool error downstream. `main` turns this into rc 2.
class ProviderUnavailableError(RuntimeError):
    pass


# meiva:note
# The database URL for this home: an installed capability, read fresh on every call. A home with no
# usable capability exits 2 and names the home and the reason, never the credential.
def read_db_url(home: Path) -> str:
    try:
        return consume_unit(home).dsn
    except (AuthorityRefusedError, ValueError, OSError) as exc:
        print(f"run-with-env: no database login for {home}: {exc}", file=sys.stderr)
        raise ProviderUnavailableError(DB_ENV) from None


# meiva:note
# The cache URL for this home: the `MEIVA_REDIS_URL` line of its `.env`, read fresh. A home whose
# `.env` has no such line exits 2 rather than handing the child an empty value.
def read_redis_url(home: Path) -> str:
    env_file = home / ".env"
    try:
        text = env_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"run-with-env: cannot read {env_file}: {exc}", file=sys.stderr)
        raise ProviderUnavailableError(REDIS_ENV) from None
    for line in text.splitlines():
        if line.startswith(f"{REDIS_ENV}="):
            value = line.split("=", 1)[1].strip().strip("\"'")
            if value:
                return value
    print(f"run-with-env: {env_file} declares no {REDIS_ENV}", file=sys.stderr)
    raise ProviderUnavailableError(REDIS_ENV)


# meiva:note
# The child environment: the caller's own, with the requested values pinned. Overwrites rather than
# defaults - both are read at call time, so a stale inherited copy must not win.
def child_env(environ: Mapping[str, str], values: Mapping[str, str]) -> dict[str, str]:
    child = dict(environ)
    child.update(values)
    return child


# meiva:note
# `--provide` names what to inject; anything else is a usage error rather than a silently ignored
# word, because a caller that asked for a value and did not get it would run an unauthenticated or
# URL-less command and see a confusing tool error instead of this one.
def parse_providers(raw: str) -> list[str]:
    wanted = [part.strip() for part in raw.split(",") if part.strip()]
    unknown = [part for part in wanted if part not in PROVIDERS]
    if unknown or not wanted:
        raise ValueError(f"unknown provider(s) {unknown or raw!r}; choose from {PROVIDERS}")
    return wanted


def collect(home: Path, providers: Sequence[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for provider in providers:
        if provider == "db":
            values[DB_ENV] = read_db_url(home)
        else:
            values[REDIS_ENV] = read_redis_url(home)
    return values


# meiva:note
# The command after the literal `--`. Missing the separator is a usage error, never an empty
# command: running nothing must not look like a successful no-op.
def command_from(argv: Sequence[str]) -> list[str]:
    if "--" not in argv:
        return []
    index = list(argv).index("--")
    return list(argv[index + 1 :])


# meiva:note
# Entry: `--check` answers presence per provider and exits; otherwise exec the caller's command with
# the values in its environment. `--check` prints one line per provider and never a value.
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run-with-env",
        description="Run a command with this home's database and cache URLs in its environment.",
    )
    parser.add_argument(
        "--home", type=Path, default=None, help="the meiva home (default: resolved)"
    )
    parser.add_argument(
        "--provide",
        default="db,redis",
        help="comma-separated providers to inject: db, redis (default: db,redis)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="print <KEY>_PRESENT True/False per provider and exit instead of running a command",
    )
    args, _rest = parser.parse_known_args(argv)
    home = args.home if args.home is not None else resolve_meiva_home()
    try:
        providers = parse_providers(args.provide)
    except ValueError as exc:
        print(f"run-with-env: {exc}", file=sys.stderr)
        return 2

    if args.check:
        missing = 0
        for provider in providers:
            try:
                collect(home, [provider])
            except ProviderUnavailableError:
                print(f"{DB_ENV if provider == 'db' else REDIS_ENV}_PRESENT False")
                missing += 1
            else:
                print(f"{DB_ENV if provider == 'db' else REDIS_ENV}_PRESENT True")
        return 1 if missing else 0

    command = command_from(sys.argv[1:] if argv is None else argv)
    if not command:
        print(
            "run-with-env: usage: run_with_env.py [--home PATH] [--provide db,redis] [--check] "
            "-- <command...>",
            file=sys.stderr,
        )
        return 2

    try:
        values = collect(home, providers)
    except ProviderUnavailableError:
        return 2
    env = child_env(os.environ, values)
    print(f"run-with-env: injected {','.join(providers)}; exec {command[0]}", file=sys.stderr)
    try:
        os.execvpe(command[0], command, env)  # noqa: S606
    except OSError as exc:
        print(f"run-with-env: cannot exec {command[0]!r}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
