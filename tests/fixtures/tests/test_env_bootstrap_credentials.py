# meiva:note
# The suite must not inherit the credentials of the process that started it: on a developer's
# box a launcher and the operator's login shell both deliver real ones, CI delivers none, so a
# test that reads one asserts a different world depending on where it ran. The snapshot check
# is the part that covers the FUTURE — a credential nobody has decided about reddens here,
# with its name, instead of turning two unrelated tests red on one machine (#5532).

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from base.host.proc import run_bounded
from tests.fixtures import env_bootstrap

_PROBE = Path(__file__).with_name("child_env_bootstrap_credentials.py")

# meiva:note
# One name per delivery path the block under test claims: the launcher's machine token, the
# provisioning token, the .env credentials the login shell exports, and the marker that
# selects the launcher's profile.
_INVENTED_NAMES = (
    "MEIVA_ALERTS_WEBHOOK_TOKEN",
    "MEIVA_API_TOKEN",
    "MEIVA_CF_API_TOKEN",
    "MEIVA_CLUSTER_SECRET",
    "MEIVA_FEISHU_APP_SECRET",
    "MEIVA_IMPERSONATION_RELAY_TOKEN",
    "MEIVA_PROCESS_PROFILE",
)


# meiva:note
# On a machine that carries no such variable (CI) the snapshot is empty and this passes
# vacuously - which is the point: the leak only exists where a launcher delivered one, and
# that is the machine the suite used to fail on (#5532). The child-process test below is the
# half that runs everywhere.
def test_no_ambient_credential_value_survives_the_bootstrap() -> None:
    survived = env_bootstrap.survived_credential_names()
    assert not survived, (
        "these credentials still hold the value the launching process gave this run: "
        f"{survived}. Decide each one in tests/fixtures/env_bootstrap.py: pop it (absent is "
        "the state CI runs in) or replace it with the sentinel the suite needs, then add a "
        "line to that block's comment saying which and why."
    )


# meiva:note
# The two FALSE cases are why this is a test: a token budget (`..._TOKENS`) and a path TO a
# credential (`..._CREDENTIALS_FILE`) must not be swept in, or a stray one on a developer's
# box arrives as noise and the whole check gets ignored. `DEEPSEEK_API_KEY` is the provider
# key `_TEST_PROVIDER_KEY_ENVS` handles; it is not MEIVA_'s to scrub, so it is not ours.
@pytest.mark.parametrize(
    ("name", "is_credential"),
    [
        ("MEIVA_API_TOKEN", True),
        ("MEIVA_ALERTS_WEBHOOK_TOKEN", True),
        ("MEIVA_CLUSTER_SECRET", True),
        ("MEIVA_FEISHU_APP_SECRET", True),
        ("MEIVA_DB_CAPABILITY_KEY", True),
        ("MEIVA_REDIS_ADMIN_PASSWORD", True),
        ("MEIVA_PROCESS_PROFILE", True),
        ("MEIVA_LLM_SILENT_IDLE_MAX_OUTPUT_TOKENS", False),
        ("MEIVA_BACKUP_OFFSITE_CREDENTIALS_FILE", False),
        ("DEEPSEEK_API_KEY", False),
        ("MEIVA_GATEWAY_URL", False),
    ],
)
def test_only_a_whole_name_element_makes_a_credential(name: str, is_credential: bool) -> None:
    assert env_bootstrap.is_credential_name(name) is is_credential


# meiva:note
# The deterministic half: a child that really does arrive carrying these values imports the
# bootstrap and comes back without them, so the check has teeth on CI too. `replaced` is a
# pass as well as `absent`: what must not survive is the value this process brought.
def test_a_leaked_credential_does_not_survive_the_bootstrap() -> None:
    env = {**os.environ, **{name: "invented-" + name for name in _INVENTED_NAMES}}
    result = run_bounded(
        [sys.executable, str(_PROBE), *_INVENTED_NAMES],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    outcome = report["outcome"]
    assert set(outcome) == set(_INVENTED_NAMES), report
    # meiva:note
    # `absent` would also be the report of a child that never carried the value, so the
    # snapshot the bootstrap took has to show it arrived — otherwise this test passes
    # vacuously the day the environment plumbing breaks.
    missing = sorted(set(_INVENTED_NAMES) - set(report["seen"]))
    assert not missing, f"the child never carried these values: {missing}"
    leaked = sorted(name for name, state in outcome.items() if state == "LEAKED")
    assert not leaked, f"the bootstrap handed these straight through: {leaked}"
