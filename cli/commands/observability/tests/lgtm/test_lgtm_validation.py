# meiva:note
# The pinned Loki parser is asked to verify the rendered config by running the installed binary.
#
# The binary is a shell script the test installs at the path converge expects, so the argv, the
# timeout and the exit status are those of a real child process.

from __future__ import annotations

from pathlib import Path

import pytest

from base.host.system.tests.fake_commands import LINUX_X86
from cli.commands.observability import lgtm_native
from cli.commands.observability.tests.lgtm.lgtm_harness import host_on


def install_loki(home: Path, script: str) -> Path:
    binary = home / "lgtm/native/bin/loki"
    binary.parent.mkdir(parents=True)
    binary.write_text(script, encoding="utf-8")
    binary.chmod(0o755)
    return binary


def test_a_config_the_parser_accepts_passes(tmp_path: Path) -> None:
    home = tmp_path.resolve()
    argv_file = home / "argv.txt"
    install_loki(home, f'#!/bin/sh\nprintf "%s\\n" "$@" > "{argv_file}"\n')

    lgtm_native.verify_loki(home, host=host_on(LINUX_X86))

    assert argv_file.read_text(encoding="utf-8").splitlines() == [
        f"-config.file={home}/lgtm/native/config/loki.yaml",
        "-verify-config",
    ]


def test_a_config_the_parser_rejects_blocks_preparation_with_its_complaint(tmp_path: Path) -> None:
    home = tmp_path.resolve()
    install_loki(home, "#!/bin/sh\necho 'unknown config field' >&2\nexit 1\n")

    with pytest.raises(RuntimeError, match="Loki config verification failed: unknown config field"):
        lgtm_native.verify_loki(home, host=host_on(LINUX_X86))
