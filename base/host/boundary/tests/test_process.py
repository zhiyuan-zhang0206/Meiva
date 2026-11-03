# meiva:note
# `base.host.boundary.process`: a finished child's streams read as text.

from __future__ import annotations

import subprocess

import pytest

from base.host.boundary.process import stderr_text, stdout_text


def test_a_text_stream_is_read_as_it_is_and_an_uncaptured_one_is_empty() -> None:
    done = subprocess.CompletedProcess[object]([], 0, "out\n", None)
    assert stdout_text(done) == "out\n"
    assert stderr_text(done) == ""


def test_the_error_a_checked_run_raised_is_read_the_same_way() -> None:
    failure = subprocess.CalledProcessError(1, ["x"], output="o", stderr="e")
    assert (stdout_text(failure), stderr_text(failure)) == ("o", "e")


def test_a_bytes_stream_means_text_true_was_forgotten() -> None:
    done = subprocess.CompletedProcess[object]([], 0, b"out", b"err")
    with pytest.raises(TypeError, match="was text=True passed"):
        stdout_text(done)
    with pytest.raises(TypeError, match="was text=True passed"):
        stderr_text(done)
