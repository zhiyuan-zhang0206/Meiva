# meiva:note
# Which ini a pgbouncer command line names, in the two shapes the platforms show. Neither
# field alone identifies the pooler on both platforms; resolving the argument against the
# working directory is the one rule that reads both.

from __future__ import annotations

from cli.commands.data_plane.pooler.identity import anchored_ini, ini_argument

_LINUX_ARGV = ["pgbouncer", "-d", "/home/meiva/pgbouncer/pgbouncer.ini"]
_MACOS_ARGV = ["/opt/homebrew/opt/pgbouncer/bin/pgbouncer", "-d", "pgbouncer.ini"]


def test_a_linux_command_line_keeps_the_absolute_ini() -> None:
    argument = ini_argument(_LINUX_ARGV)

    assert argument == "/home/meiva/pgbouncer/pgbouncer.ini"
    assert anchored_ini(argument, "/somewhere/else") == "/home/meiva/pgbouncer/pgbouncer.ini"


def test_a_macos_command_line_is_resolved_against_the_config_directory_it_changed_into() -> None:
    argument = ini_argument(_MACOS_ARGV)

    assert argument == "pgbouncer.ini"
    assert anchored_ini(argument, "/home/meiva/pgbouncer") == "/home/meiva/pgbouncer/pgbouncer.ini"


def test_a_command_line_without_a_config_argument_names_no_ini() -> None:
    assert ini_argument(["pgbouncer"]) is None
    assert ini_argument(["pgbouncer", "-d"]) is None
    assert ini_argument([]) is None


# meiva:note
# `~/.meiva` is a prefix of `~/.meiva-preview`: the anchored paths differ as whole paths, so a
# comparison of them never hands one home the other's pooler.
def test_a_sibling_home_sharing_a_path_prefix_anchors_to_a_different_ini() -> None:
    ours = anchored_ini("pgbouncer.ini", "/home/u/.meiva/pgbouncer")
    sibling = anchored_ini("pgbouncer.ini", "/home/u/.meiva-preview/pgbouncer")

    assert ours != sibling
