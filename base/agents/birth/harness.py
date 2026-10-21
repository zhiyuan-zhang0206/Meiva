from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple, cast

import psycopg

from base.api_contracts.agent_contract import (
    ForkHarnessMismatch,
    Harness,
    HarnessNotLaunchable,
    HarnessStatus,
)
from base.db import fetch_one
from base.host.env.harness_binaries import (
    HARNESS_BIN_DIRS,
    harness_child_path,
    locate_harness_binary,
)

DEFAULT_HARNESS = Harness.MEIVA

# meiva:note
# A harness enters this set when a launcher that can start a session on it
# exists. Until then the runner refuses to wake a row born on it: recording an
# intent the platform cannot honour would make the stored value a claim about a
# runtime loop that is not actually running. Every external harness is here
# because the graph routes its turns to `harness_turn`, whose bridge opens an
# adapter session; removing a value retires its launcher and leaves rows born on
# it inert, which is the rollback.
# meiva:link base/agents/docs/harness/harness.meiva.okf.md
LAUNCHABLE_HARNESSES: frozenset[Harness] = frozenset(
    {Harness.MEIVA, Harness.CLAUDE_CODE, Harness.CODEX, Harness.ANTIGRAVITY}
)


class HarnessProbe(NamedTuple):
    harness: Harness
    status: HarnessStatus
    reason: str
    binary: str | None = None
    on_path: bool = False


@dataclass(frozen=True)
class _LoginEvidence:
    signed_in: bool | None
    detail: str


# meiva:note
# Install directories a user-level CLI lands in even when the process PATH is
# trimmed. Agents start without the interactive shell profile, so a missing PATH
# entry alone is not proof that the harness is absent.
# meiva:link base/agents/docs/harness/harness.meiva.okf.md


def coerce_harness(value: str | Harness | None) -> Harness:
    if value is None:
        return DEFAULT_HARNESS
    if isinstance(value, Harness):
        return value
    try:
        return Harness(value)
    except ValueError:
        legal = ", ".join(harness.value for harness in Harness)
        raise ValueError(f"unknown harness {value!r}; legal values: {legal}") from None


# meiva:note
# Birth is the last moment this choice is cheap to refuse: a stored harness is
# immutable, so a value no launcher can start would leave an agent that can
# neither run nor move onto another loop, and only a force terminate could end
# it. A fork inherits its source's harness, so a source that never got a
# launcher is refused by the same rule instead of copying the dead value.
# meiva:link base/agents/docs/harness/harness.meiva.okf.md
def _require_launchable(
    harness: Harness, fork_from: int | None, launchable: Collection[Harness]
) -> Harness:
    if harness in launchable:
        return harness
    allowed = ", ".join(sorted(item.value for item in launchable))
    if fork_from is None:
        raise HarnessNotLaunchable(
            f"harness {harness.value!r} has no launcher yet: nothing in this release can run "
            f"an agent on it, and a stored harness never changes, so the row could never run. "
            f"Spawn on one of {allowed} instead"
        )
    raise HarnessNotLaunchable(
        f"cannot fork agent {fork_from}: it was born on harness {harness.value!r}, which has "
        f"no launcher yet, and a fork keeps its source's harness, so the child could never "
        f"run either. Fork an agent on one of {allowed}, or spawn a new one"
    )


# meiva:note
# A fork continues its source's identity, so it runs the source's harness. Naming
# a different one is refused here, inside the birth transaction, so a caller that
# skipped request validation still cannot fork across harnesses; omitting the
# value inherits the source's.
# meiva:link base/agents/docs/harness/harness.meiva.okf.md
def resolve_birth_harness(
    cur: psycopg.Cursor[Any],
    requested: str | Harness | None,
    fork_from: int | None,
    *,
    launchable: Collection[Harness] = LAUNCHABLE_HARNESSES,
) -> str:
    if fork_from is None:
        return _require_launchable(coerce_harness(requested), fork_from, launchable).value
    cur.execute("SELECT harness FROM agents_meta WHERE id = %s", (fork_from,))
    row = fetch_one(cur, "harness: read fork source")
    source = Harness(row[0])
    if requested is None:
        return _require_launchable(source, fork_from, launchable).value
    requested_harness = coerce_harness(requested)
    if requested_harness is not source:
        raise ForkHarnessMismatch(
            f"cannot fork agent {fork_from} onto harness {requested_harness.value!r}: the "
            f"source runs on {source.value!r}, and harness is an immutable property of an "
            f"agent. Spawn a new agent with harness={requested_harness.value!r} instead of "
            f"forking onto it."
        )
    return _require_launchable(source, fork_from, launchable).value


def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _has_value(mapping: object, key: str) -> bool:
    if not isinstance(mapping, dict):
        return False
    return bool(cast("dict[str, object]", mapping).get(key))


def _nested(value: object, key: str) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    inner = cast("dict[str, object]", value).get(key)
    return cast("dict[str, object]", inner) if isinstance(inner, dict) else None


def _epoch_ms(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _iso_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


# meiva:note
# A credential store answers the login question only as far as it can be read. A
# token it holds is evidence; an account name it remembers is not, because that
# record outlives the login: an ubuntu machine kept `oauthAccount` in
# `~/.claude.json` while `claude auth status` reported `loggedIn: false`, and the
# probe called it available. Where the store records an expiry, the one that
# decides is the deadline of a credential that cannot renew itself; a past
# access-token expiry only schedules a refresh, so reading it as a logout would
# mark an idle machine unavailable. `_session_deadline` takes such a deadline and
# `_renewable_credential` reports a renewable one as the age of the stored token.
# meiva:link base/agents/docs/harness/harness.meiva.okf.md
def _session_deadline(deadline: datetime | None, *, where: str) -> _LoginEvidence:
    if deadline is None:
        return _LoginEvidence(
            signed_in=True, detail=f"a token is stored in {where}, which records no expiry for it"
        )
    if deadline <= datetime.now(UTC):
        return _LoginEvidence(
            signed_in=False, detail=f"the credentials in {where} expired at {deadline.isoformat()}"
        )
    return _LoginEvidence(
        signed_in=True, detail=f"the credentials in {where} are valid until {deadline.isoformat()}"
    )


def _renewable_credential(
    deadline: datetime | None, *, where: str, credential: str
) -> _LoginEvidence:
    if deadline is None:
        return _LoginEvidence(signed_in=True, detail=f"a token is stored in {where}")
    return _LoginEvidence(
        signed_in=True,
        detail=f"a token is stored in {where}; its {credential} runs out at "
        f"{deadline.isoformat()} and is renewed on use",
    )


def _claude_credentials(record: object) -> _LoginEvidence:
    where = "~/.claude/.credentials.json"
    oauth = _nested(record, "claudeAiOauth")
    if oauth is None:
        return _LoginEvidence(signed_in=None, detail=f"{where} is unreadable")
    if not _has_value(oauth, "accessToken") and not _has_value(oauth, "refreshToken"):
        return _LoginEvidence(signed_in=False, detail=f"{where} holds no token")
    if _has_value(oauth, "refreshToken"):
        return _session_deadline(_epoch_ms(oauth.get("refreshTokenExpiresAt")), where=where)
    return _session_deadline(_epoch_ms(oauth.get("expiresAt")), where=where)


# meiva:note
# This row reports one thing: a token set that `~/.claude/.credentials.json`
# stores and whose refresh token has not expired. An account named in
# `~/.claude.json` is not that evidence — the record outlives the login — so on its
# own it reads `unknown`, as does a missing or unreadable store.
# meiva:link base/agents/docs/harness/harness.meiva.okf.md
def _claude_code_login(home: Path) -> _LoginEvidence:
    credentials = home / ".claude" / ".credentials.json"
    if credentials.is_file():
        return _claude_credentials(_read_json(credentials))
    account = _read_json(home / ".claude.json")
    if _has_value(account, "oauthAccount"):
        return _LoginEvidence(
            signed_in=None,
            detail="~/.claude.json names the account that signed in but stores no token, and "
            "that record outlives the login",
        )
    if (home / ".claude.json").is_file():
        return _LoginEvidence(signed_in=False, detail="~/.claude.json records no signed-in account")
    return _LoginEvidence(signed_in=None, detail="no Claude Code configuration found")


# meiva:note
# This row reports a token set stored in `~/.codex/auth.json`. The file records no
# expiry for it — the access token is renewed from the refresh token, and
# `last_refresh` says when that last happened — and no threshold on that age would
# be more than a guess, so the token set is the evidence and the reason carries the
# age. A missing or unreadable file reads `unknown`; a file holding no token reads
# `unavailable`.
# meiva:link base/agents/docs/harness/harness.meiva.okf.md
def _codex_login(home: Path) -> _LoginEvidence:
    where = "~/.codex/auth.json"
    auth = home / ".codex" / "auth.json"
    if not auth.is_file():
        return _LoginEvidence(signed_in=None, detail=f"no {where}")
    record = _read_json(auth)
    if not isinstance(record, dict):
        return _LoginEvidence(signed_in=None, detail=f"{where} is unreadable")
    data = cast("dict[str, object]", record)
    if _has_value(data, "OPENAI_API_KEY"):
        return _LoginEvidence(signed_in=True, detail=f"an API key is stored in {where}")
    tokens = _nested(data, "tokens")
    holds_token = _has_value(tokens, "access_token") or _has_value(tokens, "refresh_token")
    if tokens is None or not holds_token:
        return _LoginEvidence(signed_in=False, detail=f"{where} holds no credentials")
    refreshed = data.get("last_refresh")
    age = f", last refreshed {refreshed}" if isinstance(refreshed, str) else ""
    return _LoginEvidence(signed_in=True, detail=f"a ChatGPT token set is stored in {where}{age}")


# meiva:note
# The CLI keeps its session in its own directory. `~/.gemini/jetski-standalone-oauth-token`
# is written by the IDE, not by the CLI, and the CLI does not read it: on a machine
# holding only that file and a fresh token, `agy -p` still answered "authentication
# required". Reading it would report that machine available, so the probe asks for the
# CLI's own file and nothing else.
# meiva:link base/agents/docs/harness/harness.meiva.okf.md
_ANTIGRAVITY_SESSION_FILE = ".gemini/antigravity-cli/antigravity-oauth-token"


def _antigravity_session(record: object, where: str) -> _LoginEvidence:
    token = _nested(record, "token")
    if token is None:
        return _LoginEvidence(signed_in=None, detail=f"{where} is unreadable")
    if not _has_value(token, "access_token"):
        return _LoginEvidence(signed_in=False, detail=f"{where} holds no access token")
    return _renewable_credential(
        _iso_timestamp(token.get("expiry")), where=where, credential="access token"
    )


# meiva:note
# This row reports the session file its CLI wrote, and nothing else on the machine: an
# IDE login is a different credential. A readable file holding an access token is
# `available`, and its expiry is reported as age — the CLI renews from the refresh
# token, so a past access expiry is not a logout. No file, or one that cannot be read,
# is `unknown`.
# meiva:link base/agents/docs/harness/harness.meiva.okf.md
def _antigravity_login(home: Path) -> _LoginEvidence:
    token_file = home / _ANTIGRAVITY_SESSION_FILE
    if token_file.is_file():
        return _antigravity_session(_read_json(token_file), f"~/{_ANTIGRAVITY_SESSION_FILE}")
    return _LoginEvidence(
        signed_in=None, detail=f"no Antigravity session file found at ~/{_ANTIGRAVITY_SESSION_FILE}"
    )


_LOGIN_CHECKS = {
    Harness.CLAUDE_CODE: _claude_code_login,
    Harness.CODEX: _codex_login,
    Harness.ANTIGRAVITY: _antigravity_login,
}

_BINARIES = {
    Harness.CLAUDE_CODE: "claude",
    Harness.CODEX: "codex",
    Harness.ANTIGRAVITY: "agy",
}

_UNPROBED = set(Harness) - {Harness.MEIVA} - set(_BINARIES) - set(_LOGIN_CHECKS)
if _UNPROBED:
    raise RuntimeError(
        "harnesses missing a binary or login probe entry: "
        f"{sorted(item.value for item in _UNPROBED)}"
    )


# meiva:note
# What the CLI itself says, asked through the same environment a session would be handed
# (the runner allowlist plus the install directories). The keychain and the CLI's own
# session store are readable only from the session this probe runs in — the files an
# installer leaves in the home directory are not evidence either way: a machine can answer
# with no credentials file (macbook-air, Claude Code) or hold a token file its CLI never
# reads (company-air, agy).
# meiva:link base/agents/docs/harness/harness.meiva.okf.md
_CLI_STATUS_ARGS: dict[Harness, tuple[str, ...]] = {
    Harness.CLAUDE_CODE: ("auth", "status", "--json"),
    Harness.CODEX: ("login", "status"),
    Harness.ANTIGRAVITY: ("models",),
}

# meiva:note
# `agy models` fetches the catalogue over the network (measured 2-5s); the other two are
# local reads under a second. Each bound keeps the ops call bounded when a CLI hangs on a
# login prompt or a dead network, and a timeout reads `unknown`, never "signed in".
# meiva:link base/agents/docs/harness/harness.meiva.okf.md
_CLI_TIMEOUT_S: dict[Harness, float] = {
    Harness.CLAUDE_CODE: 8.0,
    Harness.CODEX: 8.0,
    Harness.ANTIGRAVITY: 12.0,
}

CliRunner = Callable[..., "subprocess.CompletedProcess[str]"]


def _cli_environment() -> dict[str, str]:
    from base.host.env.registry import child_env

    environment = child_env("runner")
    return {**environment, "PATH": harness_child_path(path_env=environment.get("PATH"))}


def _answered_claude(done: subprocess.CompletedProcess[str]) -> _LoginEvidence:
    try:
        record = json.loads(done.stdout)
    except json.JSONDecodeError:
        return _LoginEvidence(signed_in=None, detail="claude auth status printed no JSON")
    if not isinstance(record, dict):
        return _LoginEvidence(signed_in=None, detail="claude auth status printed no login state")
    data = cast("dict[str, object]", record)
    logged_in = data.get("loggedIn")
    if logged_in is True:
        return _LoginEvidence(
            signed_in=True,
            detail=f"claude auth status reports a login ({data.get('authMethod') or 'unknown'})",
        )
    if logged_in is False:
        return _LoginEvidence(signed_in=False, detail="claude auth status reports no login")
    return _LoginEvidence(signed_in=None, detail="claude auth status reported no login state")


def _answered_codex(done: subprocess.CompletedProcess[str]) -> _LoginEvidence:
    said = f"{done.stdout}\n{done.stderr}".lower()
    if done.returncode == 0 and "logged in" in said:
        return _LoginEvidence(signed_in=True, detail="codex login status reports a login")
    if done.returncode != 0 and "not logged in" in said:
        return _LoginEvidence(signed_in=False, detail="codex login status reports no login")
    return _LoginEvidence(signed_in=None, detail="codex login status reported no login state")


def _answered_agy(done: subprocess.CompletedProcess[str]) -> _LoginEvidence:
    said = f"{done.stdout}\n{done.stderr}".lower()
    if done.returncode == 0 and done.stdout.strip():
        return _LoginEvidence(
            signed_in=True, detail="agy models listed the catalogue, which needs a login"
        )
    if done.returncode != 0 and ("sign in" in said or "authentication required" in said):
        return _LoginEvidence(signed_in=False, detail="agy models asked for a sign-in")
    return _LoginEvidence(signed_in=None, detail="agy models reported no login state")


_CLI_ANSWERS: dict[Harness, Callable[[subprocess.CompletedProcess[str]], _LoginEvidence]] = {
    Harness.CLAUDE_CODE: _answered_claude,
    Harness.CODEX: _answered_codex,
    Harness.ANTIGRAVITY: _answered_agy,
}


def _cli_login(
    harness: Harness,
    binary: str,
    *,
    runner: CliRunner = subprocess.run,
    environment: dict[str, str] | None = None,
) -> _LoginEvidence:
    args = _CLI_STATUS_ARGS[harness]
    env = _cli_environment() if environment is None else environment
    try:
        done = runner(
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT_S[harness],
            env=env,
        )
    except subprocess.TimeoutExpired:
        return _LoginEvidence(
            signed_in=None, detail=f"`{binary} {' '.join(args)}` did not answer in time"
        )
    except OSError as exc:
        return _LoginEvidence(
            signed_in=None, detail=f"`{binary} {' '.join(args)}` could not run: {exc}"
        )
    return _CLI_ANSWERS[harness](done)


def _probe_external(
    harness: Harness,
    home: Path,
    path_env: str | None,
    bin_dirs: Sequence[str],
    cli_runner: CliRunner = subprocess.run,
) -> HarnessProbe:
    binary = _BINARIES[harness]
    path, on_path = locate_harness_binary(binary, home=home, path_env=path_env, bin_dirs=bin_dirs)
    if path is None:
        searched = ", ".join(
            entry if Path(entry).is_absolute() else f"~/{entry}" for entry in bin_dirs
        )
        return HarnessProbe(
            harness, "unavailable", f"the {binary} command was not found on PATH or in {searched}"
        )
    installed = f"installed at {path}" if on_path else f"installed at {path} (not on PATH)"
    evidence = _cli_login(harness, path, runner=cli_runner)
    if evidence.signed_in is None:
        evidence = _LOGIN_CHECKS[harness](home)
    if evidence.signed_in is True:
        return HarnessProbe(harness, "available", f"{installed}; {evidence.detail}", path, on_path)
    if evidence.signed_in is False:
        return HarnessProbe(
            harness,
            "unavailable",
            f"{installed}; not signed in: {evidence.detail}. Run {binary} once and sign in",
            path,
            on_path,
        )
    return HarnessProbe(
        harness,
        "unknown",
        f"{installed}; login state unknown: {evidence.detail}. "
        f"Run {binary} once in a terminal to confirm it is signed in",
        path,
        on_path,
    )


# meiva:note
# The probe asks each CLI and falls back to the stored credential only when the CLI cannot
# answer; neither path starts a session or spends model quota, and a sign-in it cannot
# prove stays `unknown` rather than being guessed either way.
# meiva:link base/agents/docs/harness/harness.meiva.okf.md
def probe_local_harnesses(
    home: Path | None = None,
    path_env: str | None = None,
    bin_dirs: Sequence[str] | None = None,
    cli_runner: CliRunner = subprocess.run,
) -> list[HarnessProbe]:
    resolved_home = Path.home() if home is None else home
    resolved_bin_dirs = HARNESS_BIN_DIRS if bin_dirs is None else bin_dirs
    rows: list[HarnessProbe] = []
    for harness in Harness:
        if harness is Harness.MEIVA:
            rows.append(
                HarnessProbe(
                    harness, "available", "built-in runtime; needs no external CLI or login"
                )
            )
            continue
        rows.append(
            _probe_external(
                harness, resolved_home, path_env, resolved_bin_dirs, cli_runner=cli_runner
            )
        )
    return rows
