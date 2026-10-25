import subprocess
from pathlib import Path

import pytest

from base.db.code_version_gate import ProcessDbGate
from base.native_process.code_version import CodeVersionError
from base.native_process.loaded_commit import LoadedCommit
from scripts.verify import understanding_replay as replay


def _git(source: Path, *args: str) -> str:
    # meiva:note
    # Fixed Git operations only touch a test-owned repository; hooks and signing stay off.
    result = subprocess.run(  # noqa: S603
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        cwd=source,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return result.stdout.strip()


def _captured_image(source: Path, known: bool) -> LoadedCommit:
    source.mkdir()
    if known:
        _git(source, "init", "--initial-branch=main")
        _git(source, "commit", "--allow-empty", "-m", "loaded A")
    image = LoadedCommit.capture(source_root=source)
    assert (image.sha is not None) is known
    if not known:
        _git(source, "init", "--initial-branch=main")
    _git(source, "commit", "--allow-empty", "-m", "later B")
    assert image.sha != _git(source, "rev-parse", "HEAD")
    return image


def _assert_image_gate(gate: ProcessDbGate, image: LoadedCommit) -> None:
    if image.sha is None:
        with pytest.raises(CodeVersionError, match="loaded commit"):
            gate.application_name()
        return
    git_dir = image.source_root / ".git"
    parked = image.source_root.parent / "replay-git-parked"
    git_dir.rename(parked)
    try:
        with pytest.raises(CodeVersionError, match="failed"):
            gate.application_name()
    finally:
        parked.rename(git_dir)
    assert gate.application_name() == "meiva:preview:v1"
    assert gate.min_read_due()
    git_dir.rename(parked)
    try:
        assert gate.application_name() == "meiva:preview:v1"
        gate.observe_minimum(1)
        assert not gate.min_read_due()
    finally:
        parked.rename(git_dir)


@pytest.mark.parametrize("known", [True, False], ids=["captured", "unknown"])
def test_report_uses_one_captured_gated_database(tmp_path: Path, known: bool) -> None:
    image = _captured_image(tmp_path / "preview-source", known)
    gate = replay.build_replay_gate(image, process="preview")
    assert isinstance(gate, ProcessDbGate)
    _assert_image_gate(gate, image)


def test_preview_refusal_precedes_capture_and_database(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="preview container only"):
        replay.main(["report", "42"], dockerenv=tmp_path / "missing")
