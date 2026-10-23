from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent.graph.exec import output
from agent.graph.exec.output import OutputLimits


@pytest.fixture
def archive_dir(limits: OutputLimits) -> Path:
    return limits.archive_dir


def _output(count: int = 340, repeats: int = 9) -> str:
    return "".join(f"line {index:03d} {'content ' * repeats}\n" for index in range(count))


def test_default_crop_keeps_25_lines_at_each_end_and_recoverable_body(
    archive_dir: Path, limits: OutputLimits, output_now: datetime
):
    body = _output()
    wrapped = output.wrap_code_output(
        body,
        limits=limits,
        now=output_now,
        elapsed_seconds=1.0,
    ).text

    assert "line 024 " in wrapped
    assert "line 025 " not in wrapped
    assert "line 314 " not in wrapped
    assert "line 315 " in wrapped
    assert "line 339 " in wrapped
    files = list(archive_dir.glob("crop_*.txt"))
    assert len(files) == 1
    assert str(files[0]) in wrapped
    assert files[0].read_text() == body
    assert len(wrapped) < len(body)


def test_context_reference_survives_legacy_ring_churn(
    archive_dir: Path, limits: OutputLimits, output_now: datetime
):
    body = _output()
    wrapped = output.wrap_code_output(
        body,
        limits=limits,
        now=output_now,
        elapsed_seconds=1.0,
    ).text
    archive = next(archive_dir.glob("crop_*.txt"))
    for second in range(25):
        output.wrap_code_output(
            "x" * 31_000,
            limits=limits,
            now=output_now + timedelta(seconds=second),
            elapsed_seconds=1.0,
        )
    assert len(list(archive_dir.glob("exec_*.txt"))) == 20
    assert archive.read_text() == body
    assert str(archive) in wrapped


def test_referenced_archive_is_kept_when_budget_is_full(
    archive_dir: Path,
    limits: OutputLimits,
    output_now: datetime,
):
    body = _output()
    limits = replace(
        limits, crop=replace(limits.crop, exec_output_crop_archive_max_bytes=len(body.encode()))
    )
    first = output.wrap_code_output(
        body,
        limits=limits,
        now=output_now,
        elapsed_seconds=1.0,
    ).text
    archive = next(archive_dir.glob("crop_*.txt"))
    second_body = body.replace("content", "another")
    second = output.wrap_code_output(
        second_body,
        referenced_messages=[HumanMessage(content=first)],
        limits=limits,
        now=output_now,
        elapsed_seconds=1.0,
    ).text
    assert second_body in second
    assert archive.read_text() == body
    assert list(archive_dir.glob("crop_*.txt")) == [archive]


def test_unreferenced_archive_is_evicted_under_byte_budget(
    archive_dir: Path,
    limits: OutputLimits,
    output_now: datetime,
):
    body = _output()
    limits = replace(
        limits, crop=replace(limits.crop, exec_output_crop_archive_max_bytes=len(body.encode()))
    )
    output.wrap_code_output(
        body,
        limits=limits,
        now=output_now,
        elapsed_seconds=1.0,
    )
    old = next(archive_dir.glob("crop_*.txt"))
    second_body = body.replace("content", "changed")
    second = output.wrap_code_output(
        second_body,
        limits=limits,
        now=output_now,
        elapsed_seconds=1.0,
    ).text
    assert not old.exists()
    files = list(archive_dir.glob("crop_*.txt"))
    assert len(files) == 1
    assert files[0].read_text() == second_body
    assert str(files[0]) in second


def test_execute_code_argument_reference_protects_archive(
    archive_dir: Path,
    limits: OutputLimits,
    output_now: datetime,
):
    body = _output()
    limits = replace(
        limits, crop=replace(limits.crop, exec_output_crop_archive_max_bytes=len(body.encode()))
    )
    output.wrap_code_output(
        body,
        limits=limits,
        now=output_now,
        elapsed_seconds=1.0,
    )
    archive = next(archive_dir.glob("crop_*.txt"))
    call = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "execute_code",
                "args": {"code": f"meiva.files.read({str(archive)!r})"},
                "id": "read",
            }
        ],
    )
    wrapped = output.wrap_code_output(
        body,
        referenced_messages=[call],
        limits=limits,
        now=output_now,
        elapsed_seconds=1.0,
    ).text
    assert archive.exists()
    assert body in wrapped


@pytest.mark.parametrize("kind", ["thinking", "reasoning", "provider_reasoning"])
def test_reasoning_reference_protects_archive(
    archive_dir: Path,
    kind: str,
    limits: OutputLimits,
    output_now: datetime,
):
    body = _output()
    limits = replace(
        limits, crop=replace(limits.crop, exec_output_crop_archive_max_bytes=len(body.encode()))
    )
    output.wrap_code_output(
        body,
        limits=limits,
        now=output_now,
        elapsed_seconds=1.0,
    )
    archive = next(archive_dir.glob("crop_*.txt"))
    if kind == "provider_reasoning":
        message = AIMessage(content="", additional_kwargs={"reasoning_content": str(archive)})
    elif kind == "reasoning":
        message = AIMessage(
            content=[
                {"type": "reasoning", "summary": [{"type": "summary_text", "text": str(archive)}]}
            ]
        )
    else:
        message = AIMessage(content=[{"type": "thinking", "thinking": str(archive)}])
    wrapped = output.wrap_code_output(
        body,
        referenced_messages=[message],
        limits=limits,
        now=output_now,
        elapsed_seconds=1.0,
    ).text
    assert archive.exists()
    assert body in wrapped


@pytest.mark.parametrize("body", [_output(120), _output(300), "\n" * 121, "x" * 12_000])
def test_threshold_short_lines_and_single_line_do_not_create_archive(
    body: str, archive_dir: Path, limits: OutputLimits, output_now: datetime
):
    assert (
        body
        in output.wrap_code_output(
            body,
            limits=limits,
            now=output_now,
            elapsed_seconds=1.0,
        ).text
    )
    assert not archive_dir.exists()


def test_line_trigger_crops_at_one_over_the_limit(
    archive_dir: Path, limits: OutputLimits, output_now: datetime
):
    wrapped = output.wrap_code_output(
        _output(301),
        limits=limits,
        now=output_now,
        elapsed_seconds=1.0,
    ).text
    assert "line 024 " in wrapped
    assert "line 025 " not in wrapped
    assert "line 275 " not in wrapped
    assert "line 276 " in wrapped
    assert "line 300 " in wrapped
    assert len(list(archive_dir.glob("crop_*.txt"))) == 1


def test_char_trigger_crops_below_the_line_count(
    archive_dir: Path, limits: OutputLimits, output_now: datetime
):
    body = "".join(f"line {index:03d} {'y' * 330}\n" for index in range(200))
    assert len(body) > 64 * 1024
    wrapped = output.wrap_code_output(
        body,
        limits=limits,
        now=output_now,
        elapsed_seconds=1.0,
    ).text
    assert "line 024 " in wrapped
    assert "line 025 " not in wrapped
    assert "line 174 " not in wrapped
    assert "line 175 " in wrapped
    assert "line 199 " in wrapped
    files = list(archive_dir.glob("crop_*.txt"))
    assert len(files) == 1
    assert files[0].read_text() == body


def test_byte_trigger_crops_multibyte_text_below_char_and_line_triggers(
    archive_dir: Path, limits: OutputLimits, output_now: datetime
):
    body = "".join(f"line {index:03d} {'\u6d4b' * 100}\n" for index in range(250))
    assert len(body) <= 64 * 1024 < len(body.encode("utf-8"))
    wrapped = output.wrap_code_output(
        body,
        limits=limits,
        now=output_now,
        elapsed_seconds=1.0,
    ).text
    assert "line 024 " in wrapped
    assert "line 025 " not in wrapped
    assert "line 224 " not in wrapped
    assert "line 225 " in wrapped
    files = list(archive_dir.glob("crop_*.txt"))
    assert len(files) == 1
    assert files[0].read_text() == body


def test_zero_threshold_disables_soft_crop_only(
    archive_dir: Path,
    limits: OutputLimits,
    output_now: datetime,
):
    limits = replace(limits, crop=replace(limits.crop, exec_output_crop_after_lines=0))
    assert (
        _output()
        in output.wrap_code_output(
            _output(),
            limits=limits,
            now=output_now,
            elapsed_seconds=1.0,
        ).text
    )
    wrapped = output.wrap_code_output(
        "x" * 31_000,
        limits=limits,
        now=output_now,
        elapsed_seconds=1.0,
    ).text
    assert "output truncated" in wrapped
    assert list(archive_dir.glob("exec_*.txt"))
    assert not list(archive_dir.glob("crop_*.txt"))


def test_newline_spelling_and_unterminated_tail_survive(
    archive_dir: Path, limits: OutputLimits, output_now: datetime
):
    body = _output().replace("\n", "\r\n").removesuffix("\r\n")
    wrapped = output.wrap_code_output(
        body,
        limits=limits,
        now=output_now,
        elapsed_seconds=1.0,
    ).text
    assert body.splitlines(keepends=True)[0] in wrapped
    assert body.splitlines(keepends=True)[-1] in wrapped
    assert next(archive_dir.glob("crop_*.txt")).read_bytes() == body.encode()


def test_budget_counts_utf8_bytes(
    archive_dir: Path,
    limits: OutputLimits,
    output_now: datetime,
):
    body = _output().replace("content", "\u6d4b\u8bd5\u8f93\u51fa\u7ed3\u679c")
    limits = replace(
        limits, crop=replace(limits.crop, exec_output_crop_archive_max_bytes=len(body))
    )
    assert (
        body
        in output.wrap_code_output(
            body,
            limits=limits,
            now=output_now,
            elapsed_seconds=1.0,
        ).text
    )
    assert not archive_dir.exists()


def test_unavailable_archive_storage_keeps_body_without_false_recovery_path(
    archive_dir: Path,
    limits: OutputLimits,
    output_now: datetime,
):
    archive_dir.write_text("a file where the archive directory should be")
    body = _output()
    wrapped = output.wrap_code_output(
        body,
        limits=limits,
        now=output_now,
        elapsed_seconds=1.0,
    ).text
    assert body in wrapped
    assert "full output at" not in wrapped
    assert archive_dir.read_text() == "a file where the archive directory should be"


def test_head_tail_counts_are_independent_of_trigger(
    archive_dir: Path,
    limits: OutputLimits,
    output_now: datetime,
):
    limits = replace(
        limits,
        crop=replace(
            limits.crop,
            exec_output_crop_after_lines=130,
            exec_output_crop_head_lines=3,
            exec_output_crop_tail_lines=2,
        ),
    )
    wrapped = output.wrap_code_output(
        _output(),
        limits=limits,
        now=output_now,
        elapsed_seconds=1.0,
    ).text
    assert "line 002 " in wrapped and "line 003 " not in wrapped
    assert "line 337 " not in wrapped and "line 338 " in wrapped
    assert "first 3 + last 2 lines" in wrapped
