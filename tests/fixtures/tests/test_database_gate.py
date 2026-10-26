import sys
from pathlib import Path
from typing import Any

import pytest

from base.config import settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.native_process import code_version
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry import process_name


def test_gate_is_lazy_and_shared_by_native_builders(
    db_url: str,
    request: pytest.FixtureRequest,
    database_version: code_version.CodeVersion,
    pytestconfig: pytest.Config,
) -> None:
    original = database_version.loaded
    assert original.source_root == pytestconfig.rootpath
    version = database_version.get()

    forbidden_codes = {
        LoadedCommit.capture.__code__,
        code_version.first_parent_count.__code__,
    }
    calls: list[str] = []

    def profile(frame: Any, event: str, _arg: Any) -> None:
        if event == "call" and frame.f_code in forbidden_codes:
            calls.append(frame.f_code.co_name)

    owner: ProcessDbGate = request.getfixturevalue("database_gate")
    assert owner.min_read_due()
    handles = [Database.from_settings(gate=owner), request.getfixturevalue("database")]
    previous = sys.getprofile()
    sys.setprofile(profile)
    try:
        for handle in handles:
            assert handle._config.db_url == settings.data_plane.db_url
            with handle.connect() as conn, conn.cursor() as cur:
                cur.execute("SELECT current_setting('application_name')")
                row = cur.fetchone()
                assert row is not None
                assert row[0] == f"meiva:pytest:v{version}"
    finally:
        sys.setprofile(previous)
    assert calls == []
    assert not owner.min_read_due()
    assert database_version.loaded is original


def test_gate_keeps_a_fresh_budget_for_each_test(
    request: pytest.FixtureRequest,
    database_version: code_version.CodeVersion,
) -> None:
    owner: ProcessDbGate = request.getfixturevalue("database_gate")
    assert owner.min_read_due()
    assert owner.application_name() == f"meiva:pytest:v{database_version.get()}"


def test_gate_preserves_an_unknown_nonexempt_session_image(tmp_path: Path) -> None:
    version = code_version.CodeVersion(LoadedCommit(tmp_path, None))
    owner = ProcessDbGate(version=version.get, process=process_name())
    assert owner.min_read_due()
    with pytest.raises(code_version.CodeVersionError, match="loaded commit"):
        owner.application_name()
    assert owner.min_read_due()
