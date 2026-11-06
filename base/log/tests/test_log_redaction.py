# meiva:note
# base/log — what a record may carry (task #5993).
#
# Two kinds of evidence, and both are needed: the pure rule, so a regression names the exact
# string that changed, and one run through a real process's own file sink, because the rule is
# worthless if the seam that opens the sink never installs it.
#
# The child logs four shapes and the sink must show two of them masked and two untouched: a
# redaction that also ate `token_count=3` would pass every "no secret in the file" assertion while
# destroying the line a later reader needs.

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from base.log.redaction import redact_text
from base.paths import logs_dir

_CHILD = Path(__file__).with_name("redaction_child.py")
_OWN_SINK_CHILD = Path(__file__).with_name("own_sink_child.py")


def test_a_url_password_is_masked_and_the_rest_of_the_line_survives() -> None:
    line = 'export MEIVA_DB_URL="postgresql://meiva_g0_runner:fixture-pw@10.1.2.3:6433/meiva_main"'

    assert redact_text(line) == (
        'export MEIVA_DB_URL="postgresql://meiva_g0_runner:***@10.1.2.3:6433/meiva_main"'
    )


def test_a_secret_named_value_in_a_serialized_mapping_is_masked() -> None:
    dump = '{"api":{"gateway":"0e03","token":"fixture-pw"},"bundle":"055e","token_count":3}'

    assert redact_text(dump) == (
        '{"api":{"gateway":"0e03","token":"***"},"bundle":"055e","token_count":3}'
    )


# meiva:note
# The asymmetry that sets rule three's edges: masking one more non-secret costs a little
# readability, missing one real password costs an irreversible leak. So the key shape is fixed to
# upper-case-and-a-secret-suffix, and these three keep every byte.
@pytest.mark.parametrize(
    ("line", "masked"),
    [
        ("export MEIVA_API_TOKEN=fixture-assignment", "export MEIVA_API_TOKEN=***"),
        ('DB_PASSWORD="fixture-quoted"', 'DB_PASSWORD="***"'),
        ("API_SECRET: fixture-colon then text", "API_SECRET: *** then text"),
        ("MEIVA_DB_PASSWORD='fixture-single'", "MEIVA_DB_PASSWORD='***'"),
    ],
)
def test_an_environment_assignment_keeps_its_key_and_loses_its_value(
    line: str, masked: str
) -> None:
    assert redact_text(line) == masked


@pytest.mark.parametrize(
    "text",
    [
        "https://example.com/path?x=1",
        "token_count=3 tokenizer=bert token_expires_at=soon",
        "PASSWORD_POLICY=strict",
        "TOKENIZER_PATH=/x",
        "a line with no credential at all",
    ],
)
def test_a_text_without_a_credential_is_returned_unchanged(text: str) -> None:
    assert redact_text(text) is text


# meiva:note
# The composition: a fresh process that opens its own file sink must write masked records and keep
# the two lines that carry no credential byte-identical.
def test_a_process_writes_masked_records_to_its_own_file(tmp_path: Path) -> None:
    subprocess.run(  # noqa: S603
        [sys.executable, str(_CHILD), str(tmp_path)], check=True, timeout=120
    )

    written = (logs_dir(home=tmp_path) / "agent-7.log").read_text()
    messages = [json.loads(line)["record"]["message"] for line in written.splitlines()]

    assert "fixture-value-url" not in written
    assert "fixture-value-dump" not in written
    assert "fixture-value-mapping" not in written
    assert "fixture-value-argument" not in written
    assert "fixture-value-env" not in written
    # meiva:note
    # The identity the URL dials as is not the secret, and a reader diagnosing a failed dial needs
    # it: redaction stays narrow enough to keep it.
    assert "meiva_g0_runner:***@10.1.2.3:6433/meiva_main" in messages[0]
    assert messages[1].endswith('{"api":{"gateway":"0e03","token":"***"},"bundle":"055e"}')
    assert messages[2].endswith("MEIVA_API_TOKEN=***")
    assert messages[3].endswith("fetched https://example.com/path?x=1 in 12ms")
    assert messages[4].endswith("structured payload")
    assert '"token_count": 3' in written


# meiva:note
# The other half of the wiring: a unit that opens its own sink and never calls an `init_*`
# (`exec_host`, `pty_sessions`, the MCP daemon) is covered because the patcher is installed at
# `add_sink`, the one funnel every sink passes through.
def test_a_process_that_opens_its_own_sink_is_masked_too(tmp_path: Path) -> None:
    target = tmp_path / "own-sink.log"
    subprocess.run(  # noqa: S603
        [sys.executable, str(_OWN_SINK_CHILD), str(target)], check=True, timeout=120
    )

    written = target.read_text()

    assert "fixture-value-sink" not in written
    assert "MEIVA_API_TOKEN=***" in written
    assert "runner:***@10.1.2.3:6433/db" in written
