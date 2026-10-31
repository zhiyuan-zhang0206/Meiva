# meiva:note
# The environment a session forwards is the one the bound context carries, not a second read of
# `os.environ`: a test drives what a session inherits the way it drives the clock or the home, and
# a partial context fails at first use instead of guessing one.
from __future__ import annotations

import pytest

from base.agents.context import MeivaContext
from meiva.sdk_surface import process_context
from meiva.shell.sessions import process_environ, session_host


# meiva:note
# The read the shell entries share: the mapping this process's root recorded at its entry.
def test_the_process_environment_is_the_mapping_the_bound_context_carries() -> None:
    process_context.bind_context(MeivaContext(environ={"MEIVA_HOME": "/from/the/context"}))
    try:
        assert process_environ() == {"MEIVA_HOME": "/from/the/context"}
    finally:
        process_context.unbind_context()


# meiva:note
# A partial context is legal; starting a child from one is not. The failure message says what the
# refusal protects, so a reader who breaks it learns the rule from the red run (#5885).
def test_a_context_without_an_environment_is_refused_at_first_use() -> None:
    process_context.bind_context(MeivaContext())
    try:
        try:
            process_environ()
        except RuntimeError as refusal:
            assert "carries no process environment" in str(refusal), (
                "the refusal must name the value that is missing: an operator reading a red run "
                f"has to know what to fill in, and this one said {str(refusal)!r}"
            )
        else:
            pytest.fail(
                "a context that carries no environment answered one anyway — this is the ambient "
                "read coming back: a shell session or a watcher started here would inherit the "
                "environment of whatever process ran the test (its PATH, its MEIVA_HOME, its "
                "credentials) instead of the mapping its root read, which is exactly what the "
                "field exists to prevent"
            )
    finally:
        process_context.unbind_context()


# meiva:note
# The builder is a function of the mapping it is handed: the PATH the session receives ends with
# the caller's, not this process's. `PATH` is present in both, so a builder that read the process
# environment instead would answer the wrong suffix.
def test_the_session_host_forwards_the_mapping_it_is_handed() -> None:
    host = session_host({"PATH": "/only-the-caller/bin"})
    assert host.forwarded["PATH"].endswith("/only-the-caller/bin")
    assert "VIRTUAL_ENV" in host.forwarded
