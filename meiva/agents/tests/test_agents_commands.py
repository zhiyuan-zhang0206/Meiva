from pathlib import Path

from meiva import agents
from meiva.sdk_surface.tests.context_home import context_over_unit_home as context_over_unit_home


def test_agents_commands_lists_name_desc_hint_no_body(unit_home: Path):
    commands = unit_home / "commands"
    commands.mkdir()
    (commands / "recap.md").write_text(
        "---\ndescription: recap it\ninstruction-hint: a focus\n---\nSECRET BODY\n",
        encoding="utf-8",
    )

    [info] = [command for command in agents.commands() if command.name == "recap"]

    assert (info.name, info.description, info.instruction_hint) == ("recap", "recap it", "a focus")
    assert not hasattr(info, "body")
    assert str(info) == "/recap a focus  — recap it"
