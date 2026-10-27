from pathlib import Path

import pytest

from base.packages.composer_commands import CommandCatalog
from meiva import skills
from meiva.tests.skills._skills_helpers import fake_skills_dir as fake_skills_dir
from meiva.tests.skills._skills_helpers import write_skill

pytestmark = pytest.mark.usefixtures("unit_home")


def _user_command(home: Path, name: str, text: str) -> None:
    commands = home / "commands"
    commands.mkdir(parents=True, exist_ok=True)
    (commands / f"{name}.md").write_text(text, encoding="utf-8")


def test_the_catalog_merges_built_ins_user_commands_and_skills(
    unit_home: Path, fake_skills_dir: Path
) -> None:
    _user_command(unit_home, "ship", "---\ndescription: ship it\n---\nShip the release.\n")
    write_skill(fake_skills_dir, "goal", "name: goal\ndescription: set a goal")

    by_name = {
        c["name"]: c for c in CommandCatalog.system(skills=skills.names, home=unit_home).discover()
    }

    assert {"recap", "compact", "ship", "goal"} <= set(by_name)
    assert by_name["ship"] == {
        "name": "ship",
        "description": "ship it",
        "instruction_hint": "",
        "body": "Ship the release.",
        "skill_target": None,
    }
    assert by_name["goal"]["skill_target"] == "goal"
    assert by_name["goal"]["body"] == ""
    assert list(by_name) == sorted(by_name)


def test_a_user_command_file_overrides_the_skill_of_the_same_name(
    unit_home: Path, fake_skills_dir: Path
) -> None:
    write_skill(fake_skills_dir, "goal", "name: goal\ndescription: set a goal")
    _user_command(unit_home, "goal", "My own goal prompt.\n")

    by_name = {
        c["name"]: c for c in CommandCatalog.system(skills=skills.names, home=unit_home).discover()
    }

    assert by_name["goal"]["skill_target"] is None
    assert by_name["goal"]["body"] == "My own goal prompt."


def test_a_message_expands_the_way_each_kind_of_command_does(
    unit_home: Path, fake_skills_dir: Path
) -> None:
    _user_command(unit_home, "ship", "Ship the release.\n")
    write_skill(fake_skills_dir, "goal", "name: goal\ndescription: set a goal")
    assert [s["name"] for s in skills.names()] == ["goal"]

    assert CommandCatalog.system(skills=skills.names, home=unit_home).expand("/ship now") == (
        "Command /ship:\nShip the release.\nAdditional message: now"
    )
    assert CommandCatalog.system(skills=skills.names, home=unit_home).expand(
        "/goal write the plan"
    ) == (
        "/goal invokes the goal skill. Load it with meiva.help(meiva.skills.goal) and follow it for "
        "this task.\nAdditional message: write the plan"
    )
    assert CommandCatalog.system(skills=skills.names, home=unit_home).expand("/ship /goal") == (
        "Command /ship:\nShip the release.\n\n"
        "/goal invokes the goal skill. Load it with meiva.help(meiva.skills.goal) and follow it for "
        "this task."
    )
    assert (
        CommandCatalog.system(skills=skills.names, home=unit_home).expand("just chatting /ship")
        == "just chatting /ship"
    )


# meiva:note
# The user commands are the ones under the home the catalog is handed, not the process's own.
def test_the_user_commands_are_the_ones_under_the_home_the_catalog_is_handed(
    unit_home: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other-home"
    _user_command(other, "only-here", "---\ndescription: here\n---\nBody.\n")

    handed = CommandCatalog.system(skills=list, home=other).discover()
    process = CommandCatalog.system(skills=list, home=unit_home).discover()

    assert "only-here" in {c["name"] for c in handed}
    assert "only-here" not in {c["name"] for c in process}
