import pytest

from agent.graph.node_log._plan import (
    StallConfig,
    StallProbe,
    WindowMode,
    stall_probe,
    window_mode,
)


@pytest.mark.parametrize(
    ("node", "config", "probe"),
    [
        ("llm", StallConfig(0.0), None),
        ("llm", StallConfig(-1.0), None),
        ("claim", StallConfig(12.0), None),
        ("harness_turn", StallConfig(12.0), None),
        ("llm", StallConfig(12.0), StallProbe(task_dump_after_s=12.0, thread_dump_after_s=14.0)),
        ("exec", StallConfig(5.0), StallProbe(task_dump_after_s=5.0, thread_dump_after_s=7.0)),
        ("exec", StallConfig(5.0, 0.5), StallProbe(task_dump_after_s=5.0, thread_dump_after_s=5.5)),
    ],
)
def test_the_probe_arms_only_above_a_positive_threshold_and_never_for_an_idle_wait_node(
    node: str, config: StallConfig, probe: StallProbe | None
) -> None:
    assert stall_probe(node, config) == probe


@pytest.mark.parametrize(
    ("message_count", "cursor", "full_window", "mode"),
    [
        (5, 0, False, WindowMode.FULL),
        (3, 42, False, WindowMode.FULL),
        (2, 2, False, WindowMode.INCREMENTAL),
        (3, 2, False, WindowMode.INCREMENTAL),
        (2, 2, True, WindowMode.FULL),
    ],
)
def test_the_enter_snapshot_renders_the_whole_window_for_a_new_process_a_shrunk_history_or_a_request(
    message_count: int, cursor: int, full_window: bool, mode: WindowMode
) -> None:
    assert window_mode(message_count=message_count, cursor=cursor, full_window=full_window) is mode
