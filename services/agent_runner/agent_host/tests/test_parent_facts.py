from __future__ import annotations

import os
import sys
from pathlib import Path

from services.agent_runner.agent_host.daemon import read_parent_facts


def test_the_parent_facts_describe_this_process() -> None:
    facts = read_parent_facts()

    assert dict(facts.environ) == dict(os.environ)
    assert facts.cwd == Path.cwd().resolve()
    assert facts.executable == sys.executable
    assert facts.editable_roots == (Path.home() / "Meiva",)
