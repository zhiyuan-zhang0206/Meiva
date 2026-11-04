# meiva:note
# Unit and filesystem tests for deterministic `MEIVA_WS_V1` workspace packing, safe extraction
# without `extractall`, symlink ancestor escape protection, quota enforcement, and atomic rollback.
# meiva:link gateway/agents/docs/agent-transfer.meiva.okf.md

from __future__ import annotations

import hashlib
import io
import os
import tarfile
from pathlib import Path

import pytest

from base import paths
from ops.agent_transfer.archive_io import (
    abort_target_workspace,
    finalize_workspace,
    pack_workspace,
    preflight_workspace_budget,
    prepare_target_workspace,
    stage_and_activate_workspace,
)
from ops.agent_transfer.budget import (
    BUNDLE_STREAM_SLACK_BYTES,
    WORKSPACE_TRANSFER_BYTES_SETTING,
    WORKSPACE_TRANSFER_FILES_SETTING,
    WorkspaceTransferLimits,
    check_bundle_stream_size,
    max_bundle_stream_bytes,
)
from ops.agent_transfer.bundle_plan import (
    SYMLINK_NORMALIZED_MODE,
    decode_bundle,
    encode_bundle,
    validate_relpath,
    verify_disk_snapshot,
)
from ops.agent_transfer.errors import (
    WorkspaceTransferCorruptError,
    WorkspaceTransferTooLargeError,
)
from ops.rpc_schemas.transfer import WorkspaceBundleManifest, WorkspaceManifestEntry

# meiva:note
# A ceiling pair these sample workspaces sit far inside: the numbers a deployment configures are
# `settings.transfer`, so a test states the pair it drives instead of inheriting a default.
_ROOMY_LIMITS = WorkspaceTransferLimits(max_files=1_000, max_bytes=16 * 1024 * 1024)


def _seed_sample_workspace(ws_src: Path) -> None:
    (ws_src / "nested" / "sub").mkdir(parents=True)
    (ws_src / "to_user.md").write_text("# Status\nReady\n", encoding="utf-8")
    script = ws_src / "nested" / "run.sh"
    script.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    script.chmod(0o700)
    data_file = ws_src / "nested" / "sub" / "notes.bin"
    data_file.write_bytes(b"\x00\x01\x02meiva-transfer")
    (ws_src / "latest_run").symlink_to("nested/run.sh")


def _assert_sample_workspace(ws_dst: Path) -> None:
    assert (ws_dst / "to_user.md").read_text(encoding="utf-8") == "# Status\nReady\n"
    assert (ws_dst / "nested" / "sub" / "notes.bin").read_bytes() == b"\x00\x01\x02meiva-transfer"
    assert (ws_dst / "nested" / "run.sh").stat().st_mode & 0o777 == 0o700
    assert (ws_dst / "latest_run").is_symlink()
    assert str((ws_dst / "latest_run").readlink()) == "nested/run.sh"


def test_pack_and_activate_round_trip_preserves_files_modes_and_symlinks(tmp_path: Path) -> None:
    source_home = tmp_path / "source-home"
    target_home = tmp_path / "target-home"
    ws_src = paths.workspace_path(source_home, 2697)
    _seed_sample_workspace(ws_src)

    ws_dst_expected, had_existing = prepare_target_workspace(
        home=target_home, agent_id=2697, transfer_id="ab" * 16
    )
    assert had_existing is False
    assert ws_dst_expected == paths.workspace_path(target_home, 2697)

    manifest, bundle = pack_workspace(
        home=source_home,
        agent_id=2697,
        transfer_id="ab" * 16,
        source_machine="macmini",
        killed_shell_sessions=[0, 2],
        limits=_ROOMY_LIMITS,
    )
    assert (manifest.files_count, manifest.killed_shell_sessions) == (3, [0, 2])

    imported = stage_and_activate_workspace(
        home=target_home,
        agent_id=2697,
        transfer_id="ab" * 16,
        bundle_bytes=bundle,
        limits=_ROOMY_LIMITS,
    )
    assert (imported.files_count, imported.total_file_bytes) == (3, manifest.total_file_bytes)

    ws_dst = paths.workspace_path(target_home, 2697)
    _assert_sample_workspace(ws_dst)

    assert finalize_workspace(home=source_home, agent_id=2697, transfer_id="ab" * 16, role="source")
    assert not ws_src.exists()
    assert not finalize_workspace(
        home=target_home, agent_id=2697, transfer_id="ab" * 16, role="target"
    )
    assert ws_dst.is_dir()


def test_nonexistent_workspace_exports_zero_entries_and_creates_empty_dir(tmp_path: Path) -> None:
    source_home = tmp_path / "src"
    target_home = tmp_path / "dst"
    source_home.mkdir()

    manifest, bundle = pack_workspace(
        home=source_home,
        agent_id=42,
        transfer_id="cd" * 16,
        source_machine="runner-a",
        killed_shell_sessions=[],
        limits=_ROOMY_LIMITS,
    )
    assert manifest.entries == []
    assert manifest.files_count == 0
    assert manifest.total_file_bytes == 0

    stage_and_activate_workspace(
        home=target_home,
        agent_id=42,
        transfer_id="cd" * 16,
        bundle_bytes=bundle,
        limits=_ROOMY_LIMITS,
    )
    ws_dst = paths.workspace_path(target_home, 42)
    assert ws_dst.is_dir()
    assert list(ws_dst.iterdir()) == []


def test_abort_target_workspace_restores_previous_directory(tmp_path: Path) -> None:
    source_home = tmp_path / "src"
    target_home = tmp_path / "dst"
    ws_src = paths.workspace_path(source_home, 7)
    ws_src.mkdir(parents=True)
    (ws_src / "fresh.txt").write_text("new-version", encoding="utf-8")

    ws_dst = paths.workspace_path(target_home, 7)
    ws_dst.mkdir(parents=True)
    (ws_dst / "prior.txt").write_text("prior-version", encoding="utf-8")

    _manifest, bundle = pack_workspace(
        home=source_home,
        agent_id=7,
        transfer_id="ef" * 16,
        source_machine="runner-a",
        killed_shell_sessions=[],
        limits=_ROOMY_LIMITS,
    )
    stage_and_activate_workspace(
        home=target_home,
        agent_id=7,
        transfer_id="ef" * 16,
        bundle_bytes=bundle,
        limits=_ROOMY_LIMITS,
    )
    assert (ws_dst / "fresh.txt").read_text(encoding="utf-8") == "new-version"
    assert not (ws_dst / "prior.txt").exists()

    restored = abort_target_workspace(home=target_home, agent_id=7, transfer_id="ef" * 16)
    assert restored is True
    assert (ws_dst / "prior.txt").read_text(encoding="utf-8") == "prior-version"
    assert not (ws_dst / "fresh.txt").exists()


@pytest.mark.parametrize(
    "bad_path",
    [
        "",
        "/etc/passwd",
        "../outside",
        "nested/../../outside",
        "dir/",
        "a\\b",
        "a\x00b",
        "./file.txt",
        "a//b",
    ],
)
def test_validate_relpath_rejects_unsafe_paths(bad_path: str) -> None:
    with pytest.raises(WorkspaceTransferCorruptError):
        validate_relpath(bad_path)


def test_corrupted_bundle_and_identity_mismatch_fail_closed(tmp_path: Path) -> None:
    source_home = tmp_path / "src"
    ws_src = paths.workspace_path(source_home, 9)
    ws_src.mkdir(parents=True)
    (ws_src / "data.txt").write_text("hello world", encoding="utf-8")

    _manifest, bundle = pack_workspace(
        home=source_home,
        agent_id=9,
        transfer_id="11" * 16,
        source_machine="runner-a",
        killed_shell_sessions=[],
        limits=_ROOMY_LIMITS,
    )

    tampered = bundle[:-1] + bytes([bundle[-1] ^ 0xFF])
    with pytest.raises(WorkspaceTransferCorruptError, match="sha256"):
        decode_bundle(
            tampered,
            expected_agent_id=9,
            expected_transfer_id="11" * 16,
            limits=_ROOMY_LIMITS,
        )

    with pytest.raises(WorkspaceTransferCorruptError, match="identity"):
        decode_bundle(
            bundle,
            expected_agent_id=10,
            expected_transfer_id="11" * 16,
            limits=_ROOMY_LIMITS,
        )


def test_symlink_ancestor_escape_in_tar_is_rejected_without_writing_outside(tmp_path: Path) -> None:
    outside = tmp_path / "outside-victim"
    outside.mkdir()
    target_home = tmp_path / "dst"

    payload = b"pwned"
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        sym = tarfile.TarInfo(name="escape_dir")
        sym.type = tarfile.SYMTYPE
        sym.mode = SYMLINK_NORMALIZED_MODE
        sym.linkname = str(outside)
        archive.addfile(sym)

        reg = tarfile.TarInfo(name="escape_dir/evil.txt")
        reg.type = tarfile.REGTYPE
        reg.mode = 0o644
        reg.size = len(payload)
        archive.addfile(reg, io.BytesIO(payload))

    tar_bytes = tar_buf.getvalue()
    manifest = WorkspaceBundleManifest(
        agent_id=15,
        transfer_id="22" * 16,
        source_machine="runner-a",
        entries=[
            WorkspaceManifestEntry(
                relpath="escape_dir",
                kind="symlink",
                mode=SYMLINK_NORMALIZED_MODE,
                size=0,
                link_target=str(outside),
            ),
            WorkspaceManifestEntry(
                relpath="escape_dir/evil.txt",
                kind="file",
                mode=0o644,
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            ),
        ],
        files_count=1,
        total_file_bytes=len(payload),
        tar_bytes=len(tar_bytes),
        tar_sha256=hashlib.sha256(tar_bytes).hexdigest(),
    )
    bundle = encode_bundle(manifest, tar_bytes, limits=_ROOMY_LIMITS)

    with pytest.raises(WorkspaceTransferCorruptError, match="symlink ancestor"):
        stage_and_activate_workspace(
            home=target_home,
            agent_id=15,
            transfer_id="22" * 16,
            bundle_bytes=bundle,
            limits=_ROOMY_LIMITS,
        )
    assert not (outside / "evil.txt").exists()
    assert not paths.workspace_path(target_home, 15).exists()


def test_quota_limits_and_special_files_fail_closed(tmp_path: Path) -> None:
    source_home = tmp_path / "src"
    ws_src = paths.workspace_path(source_home, 19)
    ws_src.mkdir(parents=True)
    (ws_src / "big.bin").write_bytes(b"x" * 128)

    with pytest.raises(WorkspaceTransferTooLargeError) as refused:
        pack_workspace(
            home=source_home,
            agent_id=19,
            transfer_id="33" * 16,
            source_machine="runner-a",
            killed_shell_sessions=[],
            limits=WorkspaceTransferLimits(max_files=1_000, max_bytes=64),
        )
    # meiva:note
    # The refusal reports the bytes it found beside the effective ceiling and names the setting an
    # operator would raise — never a module constant.
    assert f"{WORKSPACE_TRANSFER_BYTES_SETTING})" in str(refused.value)
    assert "128 bytes, past the 64-byte remainder of the 64-byte transfer ceiling" in str(
        refused.value
    )

    fifo_path = ws_src / "pipe.fifo"
    os.mkfifo(fifo_path)
    with pytest.raises(WorkspaceTransferCorruptError, match="special file"):
        pack_workspace(
            home=source_home,
            agent_id=19,
            transfer_id="33" * 16,
            source_machine="runner-a",
            killed_shell_sessions=[],
            limits=_ROOMY_LIMITS,
        )


def test_verify_disk_snapshot_detects_content_or_count_drift() -> None:
    expected = [
        WorkspaceManifestEntry(
            relpath="a.txt",
            kind="file",
            mode=0o644,
            size=3,
            sha256="a" * 64,
        )
    ]
    with pytest.raises(WorkspaceTransferCorruptError, match="count mismatch"):
        verify_disk_snapshot(expected, [])
    altered = [
        WorkspaceManifestEntry(
            relpath="a.txt",
            kind="file",
            mode=0o644,
            size=3,
            sha256="b" * 64,
        )
    ]
    with pytest.raises(WorkspaceTransferCorruptError, match=r"mismatch at 'a\.txt'"):
        verify_disk_snapshot(expected, altered)


def test_preflight_refuses_an_over_entry_workspace_with_both_actuals(tmp_path: Path) -> None:
    ws_src = paths.workspace_path(tmp_path / "src", 21)
    (ws_src / "nested").mkdir(parents=True)
    (ws_src / "nested" / "a.txt").write_bytes(b"abc")
    (ws_src / "b.txt").write_bytes(b"de")

    with pytest.raises(WorkspaceTransferTooLargeError) as refused:
        preflight_workspace_budget(
            ws_src, limits=WorkspaceTransferLimits(max_files=2, max_bytes=1024)
        )

    # meiva:note
    # The entry axis refuses, and the message still reports the byte axis it measured on the way.
    message = str(refused.value)
    assert "3 entries against the 2-entry ceiling" in message
    assert "5 bytes against the 1024-byte ceiling" in message
    assert WORKSPACE_TRANSFER_FILES_SETTING in message
    assert WORKSPACE_TRANSFER_BYTES_SETTING in message


def test_preflight_refuses_an_over_byte_workspace_with_both_actuals(tmp_path: Path) -> None:
    ws_src = paths.workspace_path(tmp_path / "src", 23)
    ws_src.mkdir(parents=True)
    (ws_src / "big.bin").write_bytes(b"x" * 4096)

    with pytest.raises(WorkspaceTransferTooLargeError) as refused:
        preflight_workspace_budget(
            ws_src, limits=WorkspaceTransferLimits(max_files=10, max_bytes=1024)
        )

    message = str(refused.value)
    assert "1 entries against the 10-entry ceiling" in message
    assert "4096 bytes against the 1024-byte ceiling" in message


def test_preflight_passes_a_workspace_inside_the_budget_and_one_that_never_existed(
    tmp_path: Path,
) -> None:
    ws_src = paths.workspace_path(tmp_path / "src", 25)
    _seed_sample_workspace(ws_src)
    preflight_workspace_budget(ws_src, limits=_ROOMY_LIMITS)

    preflight_workspace_budget(paths.workspace_path(tmp_path / "empty", 26), limits=_ROOMY_LIMITS)


def test_decode_refuses_a_bundle_over_the_ceilings_it_was_given(tmp_path: Path) -> None:
    source_home = tmp_path / "src"
    ws_src = paths.workspace_path(source_home, 27)
    ws_src.mkdir(parents=True)
    (ws_src / "data.txt").write_bytes(b"0123456789")

    _manifest, bundle = pack_workspace(
        home=source_home,
        agent_id=27,
        transfer_id="44" * 16,
        source_machine="runner-a",
        killed_shell_sessions=[],
        limits=_ROOMY_LIMITS,
    )

    # meiva:note
    # A receiver configured below what the bundle carries refuses it and says which ceiling it read,
    # so a mismatched pair across machines is visible in the refusal instead of a bare 413.
    with pytest.raises(WorkspaceTransferTooLargeError) as refused:
        decode_bundle(
            bundle,
            expected_agent_id=27,
            expected_transfer_id="44" * 16,
            limits=WorkspaceTransferLimits(max_files=1_000, max_bytes=4),
        )
    assert "10 bytes against the 4-byte ceiling" in str(refused.value)


def test_the_stream_ceiling_follows_the_byte_ceiling_and_reports_it() -> None:
    limits = WorkspaceTransferLimits(max_files=10, max_bytes=1024)
    assert max_bundle_stream_bytes(limits) == 1024 + BUNDLE_STREAM_SLACK_BYTES

    check_bundle_stream_size(1024 + BUNDLE_STREAM_SLACK_BYTES, limits=limits)
    with pytest.raises(WorkspaceTransferTooLargeError) as refused:
        check_bundle_stream_size(1025 + BUNDLE_STREAM_SLACK_BYTES, limits=limits)
    assert f"{1025 + BUNDLE_STREAM_SLACK_BYTES} bytes" in str(refused.value)
    assert f"{1024}-byte" in str(refused.value)
    assert WORKSPACE_TRANSFER_BYTES_SETTING in str(refused.value)


def test_the_refusal_names_the_settings_that_move_the_ceilings() -> None:
    from base import config

    # meiva:note
    # This module is pure and cannot read the config, so the two setting spellings in its refusals
    # are pinned here: a renamed field or alias fails this test instead of sending an operator to a
    # setting that no longer exists.
    assert (
        f"settings.transfer.max_workspace_files ({config.field_alias('max_workspace_files')})"
    ) == WORKSPACE_TRANSFER_FILES_SETTING
    assert (
        f"settings.transfer.max_workspace_bytes ({config.field_alias('max_workspace_bytes')})"
    ) == WORKSPACE_TRANSFER_BYTES_SETTING
