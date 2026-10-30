# meiva:note
# The fake root control socket, run against the real client: a marked birth the socket names as
# this process is the unit serving, an answer naming any other process is refused, and each
# refusal `observe_root` makes has a case that reaches it.

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from base.deploy.lifecycle import start_serving
from base.deploy.lifecycle.start_serving import ServingState
from base.host.runtime_interpreter import LoadedRuntimeIdentity
from base.host.tests.short_tmp import short_tmp as short_tmp
from base.native_process.evidence import ExpectedProcess
from base.native_process.root_control.client import RootClientError
from base.native_process.root_control.tests.status_root import (
    birth_from_response,
    serve,
    write_receipt,
)


@pytest.fixture
def home(short_tmp: Path) -> Path:
    path = short_tmp / "home"
    path.mkdir()
    return path


# meiva:note
# The socket runs in this process, so the kernel-reported peer is this process: the answer's
# birth must be exactly that, and the receipt carries the same fields the answer does.
def test_the_marked_birth_is_serving_while_the_socket_names_this_process(home: Path) -> None:
    with serve(home=home) as answer:
        birth = birth_from_response(answer["root"])
        write_receipt(home, birth)

        assert birth["process"]["pid"] == os.getpid()
        assert start_serving.observe_root(home).process.pid == os.getpid()
        assert start_serving.is_serving(home) is True


# meiva:note
# The receipt is owned by `ServingState`: the file this fake writes must be the model the reader
# parses, with a birth equal field by field to the row the socket answered with. A field the
# model grows fails here first instead of failing an e2e run somewhere else.
def test_the_receipt_is_the_model_the_reader_parses(home: Path) -> None:
    with serve(home=home) as answer:
        birth = birth_from_response(answer["root"])
        write_receipt(home, birth)

        published = ServingState.model_validate_json(
            start_serving.state_path(home).read_text(encoding="utf-8")
        )

        root = answer["root"]
        assert published.state == "serving"
        assert published.birth is not None
        assert published.birth.home == root["home"]
        assert published.birth.launch_digest == root["launch_digest"]
        assert published.birth.runtime == LoadedRuntimeIdentity.model_validate(root["runtime"])
        assert published.birth.process == ExpectedProcess(
            pid=root["pid"], create_time=root["create_time"], starttime=root["starttime"]
        )


# meiva:note
# One connection carries one request, and the reader may be several processes: the socket answers
# every connection it accepts, not only the first.
def test_the_socket_answers_every_connection(home: Path) -> None:
    with serve(home=home) as answer:
        write_receipt(home, birth_from_response(answer["root"]))

        first = start_serving.observe_root(home)
        second = start_serving.observe_root(home)

        assert first.same_generation(second)
        assert start_serving.is_serving(home) is True


# meiva:note
# An answer that names another process is not this unit's root, however well-formed it is: the
# kernel-reported peer is the only authenticating fact, so the observation must raise.
def test_an_answer_naming_another_process_is_refused(home: Path) -> None:
    with serve(home=home, report_pid=os.getpid() + 1) as answer:
        write_receipt(home, birth_from_response(answer["root"]))

        with pytest.raises(RootClientError):
            start_serving.observe_root(home)
        assert start_serving.is_serving(home) is False


# meiva:note
# An otherwise valid answer that leaves the birth row out cannot name the root either.
def test_an_answer_without_the_birth_row_is_refused(home: Path) -> None:
    with serve(home=home, omit_root=True):
        with pytest.raises(RootClientError):
            start_serving.observe_root(home)
        assert start_serving.is_serving(home) is False


# meiva:note
# A root that reports itself not running is not serving this unit, even with a matching birth.
def test_an_answer_from_a_root_that_is_not_running_is_refused(home: Path) -> None:
    with serve(home=home, running=False) as answer:
        write_receipt(home, birth_from_response(answer["root"]))

        with pytest.raises(RootClientError):
            start_serving.observe_root(home)
        assert start_serving.is_serving(home) is False


# meiva:note
# One home has one root: a second owner must fail with a reason a reader can act on, not with a
# bare bind error and never by quietly becoming a reader.
def test_a_second_owner_of_the_same_home_fails_loudly(home: Path) -> None:
    with serve(home=home):
        second = subprocess.run(
            args=[sys.executable, "-m", "base.deploy.lifecycle.tests.second_root_owner", str(home)],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )

    assert second.returncode != 0
    assert "this home already has a root" in second.stdout + second.stderr
