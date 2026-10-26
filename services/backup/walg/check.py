"""`meiva backup walg check`: prove, before archiving is switched on, that it can work.

The failures this exists to surface early: a missing or edited binary, an unusable
configuration or key, and a storage credential that can write but not read or
delete (retention only deletes weeks later, so a missing Delete permission would
otherwise show up long after the setup was forgotten). The storage steps are one
round trip through WAL-G's own storage tools, under the configured prefix:
`st put` a small object, `st ls` it, `st get` it back and compare (the encryption
round trip), `st rm` it, `st ls` again to see it gone. Then, when Postgres answers,
the archive facts it reports are printed (informational: the health probe judges them).

`st put` always compresses, so the stored object is `<name>.lz4` and `st get`
takes that full name. Nothing printed here contains a credential or key value.
"""

from __future__ import annotations

import tempfile
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg

from base.cluster.dataplane import walg_binary
from services.backup.walg import config as walg_config
from services.backup.walg import probe
from services.backup.walg.archive import expected_archive
from services.backup.walg.runner import WalgCommandError, run_walg

_STORAGE_CALL_TIMEOUT_S = 120
_CHECK_DIR = "meiva-check"


@dataclass(frozen=True)
class Step:
    name: str
    detail: str
    ok: bool = field(default=True, kw_only=True)


def _storage_round_trip(*, path: Path | None, token: str) -> str:
    remote = f"{_CHECK_DIR}/{token}.txt"
    stored = f"{remote}.lz4"
    payload = f"meiva walg check {token}\n".encode()

    def call(*args: str) -> str:
        return str(run_walg(list(args), timeout_s=_STORAGE_CALL_TIMEOUT_S, path=path))

    with tempfile.TemporaryDirectory(prefix="meiva-walg-check-") as scratch:
        source, fetched = Path(scratch, "put"), Path(scratch, "get")
        source.write_bytes(payload)
        try:
            call("st", "put", str(source), remote)
            if token not in call("st", "ls", f"{_CHECK_DIR}/"):
                raise WalgCommandError("the object just written is not listed")
            call("st", "get", stored, str(fetched))
            if fetched.read_bytes() != payload:
                raise WalgCommandError("the object read back differs from the one written")
        finally:
            # Always try to leave nothing behind, then prove the delete permission.
            removal_error: WalgCommandError | None = None
            try:
                call("st", "rm", stored)
            except WalgCommandError as exc:
                removal_error = exc
        if removal_error is not None:
            raise WalgCommandError(
                f"cannot delete (is Delete granted on the prefix?): {removal_error}"
            )
        if token in call("st", "ls", f"{_CHECK_DIR}/"):
            raise WalgCommandError("the object is still listed after it was deleted")
    return "put, list, get (encryption round trip), delete, list"


def _postgres_facts(
    *,
    path: Path | None,
    admin: AbstractContextManager[psycopg.Connection[Any]],
) -> str:
    expected = expected_archive(path=path)
    try:
        with admin as conn:
            state = probe.read_archiver_state(conn)
    except Exception as exc:
        return f"not read ({type(exc).__name__}); the health probe judges it once Postgres is up"
    if expected is not None and probe.settings_differ(state, expected):
        verdict = "differs from the configuration (stop, then start this unit again, to apply)"
    else:
        verdict = "matches the configuration"
    return (
        f"archive_mode={state.archive_mode}, {verdict}; "
        f"archived={state.archived_count} failed={state.failed_count}"
    )


# meiva:note
# Run the checks in order, stopping at the first failure; the last step is the verdict. `token` names
# the object the storage round trip writes; the caller draws a fresh one per run, so two runs (or a
# run and a leftover of an earlier one) never address the same object.
def run_check(*, path: Path | None, token: str) -> list[Step]:
    return run_check_on(
        path=path,
        token=token,
        binary_problem=walg_binary.installed_problem(),
        admin=probe.admin_connection(),
    )


# meiva:note
# `run_check` over the facts it would resolve: why the installed binary is not the pinned
# build (None when it is) and the dial to the home's Postgres.
def run_check_on(
    *,
    path: Path | None,
    token: str,
    binary_problem: str | None,
    admin: AbstractContextManager[psycopg.Connection[Any]],
) -> list[Step]:
    if path is None:
        return [Step("configured", "MEIVA_WALG_CONFIG_FILE is not set", ok=False)]
    steps = [Step("configured", str(path))]

    if binary_problem is not None:
        return [*steps, Step("binary", f"{binary_problem} (meiva converge installs it)", ok=False)]
    steps.append(Step("binary", f"pinned wal-g {walg_binary.WALG_VERSION}"))

    try:
        config = walg_config.read_config(path)
        key_problem = walg_config.pin_problem(config)
    except walg_config.WalgConfigError as exc:
        return [*steps, Step("configuration", str(exc), ok=False)]
    if key_problem is not None:
        return [*steps, Step("configuration", key_problem, ok=False)]
    pinned = (
        "pinned"
        if walg_config.pinned_key_id() == config.key_fingerprint
        else "not pinned yet: converge pins it on first use"
    )
    steps.append(
        Step(
            "configuration",
            f"prefix {config.prefix}; key fingerprint {config.key_fingerprint} ({pinned}); "
            "WALG_PREVENT_WAL_OVERWRITE on",
        )
    )

    try:
        steps.append(Step("storage", _storage_round_trip(path=path, token=token)))
    except WalgCommandError as exc:
        return [*steps, Step("storage", str(exc), ok=False)]

    return [*steps, Step("postgres", _postgres_facts(path=path, admin=admin))]
