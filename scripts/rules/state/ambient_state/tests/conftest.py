from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from meizel import allowances
from meizel.roles import parse_roles, select_packages

from scripts.rules.state.ambient_state import Config
from scripts.rules.state.ambient_state.allowlist import from_policy

# meiva:note
# This repository's own roles and policy: the rule is judged against the lists it declares.
_HOST = Path(__file__).resolve().parents[5]

# meiva:note
# The directories the rule governs, read once from this repository: `measure` takes the scope as a
# value, so a test names the same declaration the gate names instead of inventing one.
SCOPE = select_packages(
    parse_roles((_HOST / "meizel.toml").read_text(encoding="utf-8"), label="meizel.toml"),
    "application",
    "templates",
)

# meiva:note
# The rule's lists as this repository declares them (`REAL_ALLOWLISTS`) and as a synthetic root sees
# them (`ALLOWLISTS`). A test root declares no file and no site of its own, so `ALLOWLISTS` empties
# the three tables whose entries name a real file or a real site; every other list — pure
# constructors, sink callees, the settings modules — stays the repository's own, because a test
# module is judged against the same constructors the gate exempts.
#
# The lists are a value now: a test that wants an entry builds the value it judges instead of
# editing a global — `replace(CONFIG, allow=replace(ALLOWLISTS, allowed={...}))` — and passes it.
# Nothing a test writes can reach another test.
REAL_ALLOWLISTS = from_policy(
    allowances.parse((_HOST / "meizel.policy.toml").read_text(encoding="utf-8"))[0]
)
ALLOWLISTS = replace(REAL_ALLOWLISTS, sink_facades={}, allowed={}, pure_repo_callees={})
CONFIG = Config(scope=SCOPE, allow=ALLOWLISTS)
