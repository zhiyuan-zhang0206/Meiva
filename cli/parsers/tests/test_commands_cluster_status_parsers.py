# meiva:note
# The cluster resume checklist names only commands that parse.

from __future__ import annotations

import re

import pytest

from cli.commands.cluster.control import cmd_cluster_resume
from cli.commands.cluster.tests.gateway_server import Reply, access_to, serving


# meiva:note
# Every `meiva ...` command the resume checklist prints must parse with the real CLI parser: an
# operator following it after an address change is exactly the person who cannot afford a step
# that exits 2. The pg_hba step names the current regeneration path: a gateway `meiva restart`
# rewrites pg_hba.conf and reloads the retained Postgres on its start leg.
def test_cmd_cluster_resume_checklist_names_only_commands_that_parse(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cli.parsers import build_parser

    route = ("POST", "/api/cluster/machines/runner-a/resume")
    with serving({route: Reply(body={"name": "runner-a", "resumed": True})}) as gateway:
        assert cmd_cluster_resume("runner-a", access=access_to(gateway.url)) == 0
    out = capsys.readouterr().out
    commands = re.findall(r"`(meiva [^`]+)`", out)
    assert commands, out
    parser = build_parser()
    for command in commands:
        parser.parse_args(command.split()[1:])  # SystemExit(2) fails the test
    assert "`meiva restart --all-services" in out
    assert "--restart-only" not in out
