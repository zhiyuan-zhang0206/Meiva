# meiva:note
# The checkpoint watcher template: what has landed, how many files it waits for, and the wake.

import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from meiva.tests.fixtures.gateway_wire import GatewayStub, GatewayWire
from meiva.tests.fixtures.gateway_wire import gateway_stub as gateway_stub
from meiva.tests.fixtures.pin_agent import pin_agent
from meiva_builtins.skills.tests.skill_script import load_skill_script

REFERENCE = Path(__file__).resolve().parents[1] / "references/gather_files.py"
_ORCHESTRATOR = 42
_MESSAGES = f"/api/agents/{_ORCHESTRATOR}/messages"
gather: Any = load_skill_script(
    "coordination", "meiva-dynamic-workflow", "references", "gather_files.py"
)


def _checkpoint(
    directory: Path,
    expected: tuple[str, ...] = (),
    *,
    glob: str = "",
    required: int = 0,
) -> Any:
    return gather.Checkpoint(str(directory), expected, glob, required, _ORCHESTRATOR)


@pytest.fixture
def orchestrator(sdk_environment: object, gateway_stub: GatewayStub) -> Iterator[GatewayWire]:
    with GatewayWire.open(gateway_stub, retries=2) as wire:
        pin_agent(7, clients=wire.context.clients)
        wire.stub.reply("POST", _MESSAGES, {"status": "enqueued"}, status=201)
        yield wire


def test_landed_lists_the_named_files_that_exist_in_the_order_named(tmp_path: Path) -> None:
    (tmp_path / "b.json").write_text("{}")
    (tmp_path / "a.json").write_text("{}")

    assert gather.landed(_checkpoint(tmp_path, ("b.json", "missing.json", "a.json"))) == [
        "b.json",
        "a.json",
    ]


def test_landed_by_glob_lists_sorted_matches_whatever_the_names(tmp_path: Path) -> None:
    for name in ("w5_feedback_2.json", "w5_feedback_1.json", "w4_other.json"):
        (tmp_path / name).write_text("{}")

    checkpoint = _checkpoint(tmp_path, glob="w5_feedback_*.json", required=2)

    assert gather.landed(checkpoint) == ["w5_feedback_1.json", "w5_feedback_2.json"]


def test_threshold_is_all_named_files_unless_a_count_is_given(tmp_path: Path) -> None:
    assert gather.threshold(_checkpoint(tmp_path, ("a", "b", "c"))) == 3
    assert gather.threshold(_checkpoint(tmp_path, ("a", "b", "c"), required=2)) == 2
    assert gather.threshold(_checkpoint(tmp_path, glob="*.json", required=4)) == 4


def test_a_glob_without_a_count_and_an_empty_checkpoint_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="needs an explicit REQUIRED_COUNT"):
        gather.threshold(_checkpoint(tmp_path, glob="*.json"))
    with pytest.raises(ValueError, match="configure EXPECTED_FILES"):
        gather.threshold(_checkpoint(tmp_path))


def test_the_wake_message_names_the_directory_the_count_and_the_files(tmp_path: Path) -> None:
    message = gather.wake_message(_checkpoint(tmp_path), ["a.json", "b.json"], 3)

    assert message == f"checkpoint reached in {tmp_path} (2/3): a.json, b.json"


def test_a_k_of_n_checkpoint_wakes_when_the_kth_file_lands(
    orchestrator: GatewayWire, tmp_path: Path
) -> None:
    (tmp_path / "a.json").write_text("{}")
    checkpoint = _checkpoint(tmp_path, ("a.json", "b.json", "c.json"), required=2)

    def second_file_lands() -> None:
        (tmp_path / "b.json").write_text("{}")

    timer = threading.Timer(0.1, second_file_lands)
    timer.start()
    try:
        gather.watch(checkpoint, 0.01)
    finally:
        timer.cancel()
        timer.join()

    [sent] = orchestrator.sent
    assert sent.path == _MESSAGES
    assert sent.body["content"] == f"checkpoint reached in {tmp_path} (2/2): a.json, b.json"


def test_an_unconfigured_template_refuses_to_watch(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="configure EXPECTED_FILES"):
        gather.watch(_checkpoint(tmp_path), 0)


# meiva:note
# Every orchestrator arms a checkpoint by replacing these exact placeholder assignments in this file's text (the docstring may repeat one, which the same replacement touches harmlessly).
def test_the_placeholders_the_orchestrators_substitute_appear_once_each() -> None:
    lines = REFERENCE.read_text(encoding="utf-8").splitlines()

    for placeholder in (
        'HANDOFF_DIR = ""',
        "EXPECTED_FILES: list[str] = []",
        'MATCH_GLOB = ""',
        "REQUIRED_COUNT = 0",
        "ORCHESTRATOR_ID = 0",
    ):
        assert sum(line.startswith(placeholder) for line in lines) == 1, placeholder
