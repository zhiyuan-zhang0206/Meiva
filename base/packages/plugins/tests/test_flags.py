from pathlib import Path

import pytest

from base.config import settings
from base.host.env.process_profile import MEIVA_PROCESS_PROFILE_ENV
from base.packages.plugins.flags import (
    UndeclaredFlag,
    UnknownFlag,
    read_declared_flag,
    validate_flag_key,
)
from base.packages.plugins.tests.plugin_boundary import run_boundary

FLAG = "daemon.notice_ttl_limit_seconds"


@pytest.mark.parametrize(
    "key",
    [
        "nodot",
        "a.b.c",
        "",
        "bogus.x",
        "agent.bogus_field",
        "agent.notice_ttl_limit_seconds",
        "data_plane.db_url",
    ],
)
def test_invalid_or_sensitive_core_dependency_is_rejected(key: str) -> None:
    with pytest.raises(UnknownFlag) as caught:
        validate_flag_key(key)
    assert repr(key) in str(caught.value)


@pytest.mark.parametrize("key", ["bogus.x", "data_plane.db_url"])
def test_read_validates_the_whole_supplied_declaration(key: str) -> None:
    with pytest.raises(UnknownFlag):
        read_declared_flag(FLAG, (FLAG, key))


def test_read_requires_the_explicit_core_dependency() -> None:
    with pytest.raises(UndeclaredFlag):
        read_declared_flag(FLAG, ())


def test_declared_dependency_reads_the_constructed_process_domain() -> None:
    assert validate_flag_key(FLAG) == FLAG
    assert read_declared_flag(FLAG, (FLAG,)) == settings.daemon.notice_ttl_limit_seconds


def test_unavailable_domain_is_rejected_without_default(tmp_path: Path) -> None:
    run_boundary(
        "unavailable-flag", home=tmp_path, env={MEIVA_PROCESS_PROFILE_ENV: "gateway"}, args=[]
    )
