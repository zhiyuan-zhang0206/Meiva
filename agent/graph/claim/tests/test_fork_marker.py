from __future__ import annotations

from pathlib import Path

from agent.graph.claim._dispatch import fork_marker_text


def test_a_native_fork_marker_names_no_directory() -> None:
    text = fork_marker_text("[t] ", "agent:7", 9, None)
    assert text.startswith("[t] You have been forked from agent:7. ")
    assert text.endswith("Continue as agent 9.")
    assert "working directory" not in text


def test_an_external_fork_marker_names_the_childs_workspace() -> None:
    text = fork_marker_text("", "agent:7", 9, Path("/home/meiva/workspaces/9"))
    assert text.endswith(
        "Continue as agent 9. Your working directory is now /home/meiva/workspaces/9."
    )
