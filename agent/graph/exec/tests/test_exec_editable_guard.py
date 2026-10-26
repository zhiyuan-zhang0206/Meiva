from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import UUID

from agent.graph.exec._guard import inspection_refusal, violation_refusal
from agent.graph.exec._result import ExecChildError, ExecDone
from agent.graph.exec._subprocess import run_in_subprocess
from agent.graph.exec.tests.process_inputs import real_parent
from base.db.code_version_gate import ProcessDbGate
from base.deploy.release import editable_install
from meiva.tests.fixtures.pin_agent import exec_context

_AGENT_ID = 424242


def test_a_poisoned_install_names_its_records_and_forbids_a_retry(tmp_path: Path) -> None:
    poisoned_pth = tmp_path / "prod" / ".venv" / "_editable_impl_meiva.pth"
    violation = f"{poisoned_pth} names {str(tmp_path / 'deleted-worktree')!r}"

    refusal = violation_refusal((violation,))

    assert refusal is not None
    assert isinstance(refusal.exc, ExecChildError)
    assert refusal.exc.exc_type == "exec_editable_install_poisoned"
    assert violation in refusal.output
    assert "no child was started" in refusal.output
    assert "do not retry" in refusal.output.lower()


def test_a_healthy_install_is_not_refused() -> None:
    assert violation_refusal(()) is None


def test_an_inspection_failure_tells_the_agent_not_to_retry() -> None:
    failure = PermissionError("site-packages is read-only")

    refusal = inspection_refusal(failure)

    assert refusal.exc is failure
    assert "site-packages is read-only" in refusal.output
    assert "do not retry" in refusal.output.lower()


async def test_a_system_interpreter_has_no_checkout_to_guard(
    tmp_path: Path, database_gate: ProcessDbGate
) -> None:
    result, _payload = await run_in_subprocess(
        "print('healthy child')",
        exec_context(_AGENT_ID),
        asyncio.Event(),
        30.0,
        exec_dir=tmp_path / "exec",
        parent=real_parent(),
        accumulation_max_chars=1_000_000,
        request_id=UUID(int=1),
        result_id=UUID(int=2),
    )

    assert isinstance(result, ExecDone)
    assert result.output == "healthy child\n"


async def test_a_real_poisoned_checkout_refuses_the_run_without_repairing_files(
    tmp_path: Path, database_gate: ProcessDbGate
) -> None:
    source = tmp_path / "source"
    packages = source / ".venv" / "lib" / "python3.12" / "site-packages"
    packages.mkdir(parents=True)
    pointer = packages / editable_install.EDITABLE_PTH_NAME
    pointer.write_text(str(tmp_path / "removed-checkout"))
    metadata = packages / "meiva-0.1.5.dist-info" / "direct_url.json"
    metadata.parent.mkdir()
    metadata.write_text(
        json.dumps(
            {"url": (tmp_path / "removed-checkout").as_uri(), "dir_info": {"editable": True}}
        )
    )
    before = {path: path.read_bytes() for path in (pointer, metadata)}

    result, payload = await run_in_subprocess(
        "print('must never run')",
        exec_context(_AGENT_ID),
        asyncio.Event(),
        30.0,
        exec_dir=tmp_path / "exec",
        parent=real_parent(source_root=source, editable_roots=()),
        accumulation_max_chars=1_000_000,
        request_id=UUID(int=1),
        result_id=UUID(int=2),
    )

    assert payload is None
    assert "no child was started" in result.output
    assert {path: path.read_bytes() for path in before} == before
    assert not list((tmp_path / "exec").rglob("*.json"))
