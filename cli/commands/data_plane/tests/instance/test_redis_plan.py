# meiva:note
# The Redis plan: conf, command line, credentials and start steps are values.
#
# Redis always authenticates and its admin password never reaches argv; both are facts of what
# the pure functions return. The real daemon is started by `test_redis_start_native.py`.

from __future__ import annotations

from pathlib import Path

import pytest

from cli.commands.data_plane.instance.redis_plan import (
    AffirmAcl,
    AwaitReachableBind,
    AwaitServerReady,
    LaunchServer,
    PrepareDataDir,
    RedisLaunch,
    ReportReady,
    RequireFreePort,
    WriteConf,
    plan_redis_start,
    redis_auth_env,
    redis_bind_addrs,
    redis_conf_text,
    redis_server_argv,
    require_redis_credentials,
)

_SECRET = "s3cret-admin-password"  # noqa: S105


def test_the_conf_always_carries_the_save_schedule_and_requirepass() -> None:
    assert redis_conf_text("admin") == (
        'save 900 1\nsave 300 10\nsave 60 10000\nrequirepass "admin"\n'
    )


def test_the_conf_escapes_quotes_and_backslashes_in_the_password() -> None:
    text = redis_conf_text('a"b\\c')
    assert text.endswith('requirepass "a\\"b\\\\c"\n')


def test_an_empty_admin_password_renders_nothing() -> None:
    with pytest.raises(ValueError, match="admin password"):
        redis_conf_text("")
    with pytest.raises(ValueError, match="admin password"):
        redis_auth_env("")


def test_redis_cli_authenticates_through_the_environment_only() -> None:
    assert redis_auth_env(_SECRET) == {"REDISCLI_AUTH": _SECRET}


@pytest.mark.parametrize(("admin", "runtime"), [("", ""), ("", "runtime"), ("admin", "")])
def test_a_start_without_both_passwords_is_refused_before_any_effect(
    admin: str, runtime: str
) -> None:
    with pytest.raises(ValueError, match="password"):
        require_redis_credentials(admin, runtime)


def test_both_passwords_admit_a_start() -> None:
    require_redis_credentials("admin", "runtime")


def test_macos_redis_binds_loopback_alone_whatever_the_reach() -> None:
    declared = ("127.0.0.1", "10.0.0.5")
    assert redis_bind_addrs(macos=True, declared=declared) == ("127.0.0.1",)


def test_linux_redis_binds_the_declared_addresses() -> None:
    assert redis_bind_addrs(macos=False, declared=("127.0.0.1",)) == ("127.0.0.1",)
    declared = ("127.0.0.1", "10.0.0.5")
    assert redis_bind_addrs(macos=False, declared=declared) == declared


def _launch(bind: tuple[str, ...] = ("127.0.0.1",)) -> RedisLaunch:
    return RedisLaunch(
        server_bin="/r/redis-server",
        conf=Path("/home/redis/redis.conf"),
        port=6380,
        bind_addrs=bind,
        data=Path("/home/redis"),
    )


def test_the_daemon_command_line_names_the_conf_and_never_the_password() -> None:
    argv = redis_server_argv(_launch())
    assert argv[:2] == ["/r/redis-server", "/home/redis/redis.conf"]
    assert argv[argv.index("--daemonize") + 1] == "yes"
    assert argv[argv.index("--port") + 1] == "6380"
    assert argv[argv.index("--dir") + 1] == "/home/redis"
    assert argv[argv.index("--logfile") + 1] == "/home/redis/redis.log"
    assert "--requirepass" not in argv and "-a" not in argv


def test_every_bind_address_follows_the_bind_flag() -> None:
    argv = redis_server_argv(_launch(("127.0.0.1", "10.0.0.5")))
    start, end = argv.index("--bind"), argv.index("--protected-mode")
    assert argv[start:end] == ["--bind", "127.0.0.1", "10.0.0.5"]


def test_persistence_rides_the_conf_not_the_command_line() -> None:
    assert "--save" not in redis_server_argv(_launch())


def test_a_running_redis_only_has_its_acl_and_conf_re_affirmed() -> None:
    assert plan_redis_start(running=True, macos=False, off_box_host="10.0.0.5") == (
        AffirmAcl(),
        WriteConf(),
        ReportReady(already_running=True),
    )


def test_a_stopped_single_box_redis_is_launched_then_provisioned() -> None:
    assert plan_redis_start(running=False, macos=False, off_box_host=None) == (
        RequireFreePort(),
        PrepareDataDir(),
        WriteConf(),
        LaunchServer(),
        AwaitServerReady(),
        AffirmAcl(),
        ReportReady(already_running=False),
    )


def test_a_stopped_linux_redis_on_a_declared_reach_waits_for_the_address() -> None:
    steps = plan_redis_start(running=False, macos=False, off_box_host="10.0.0.5")
    assert steps[:2] == (RequireFreePort(), AwaitReachableBind("10.0.0.5"))


def test_macos_redis_never_waits_for_the_reachable_address() -> None:
    steps = plan_redis_start(running=False, macos=True, off_box_host="10.0.0.5")
    assert not [step for step in steps if isinstance(step, AwaitReachableBind)]


def test_the_acl_user_is_provisioned_only_after_the_server_answers() -> None:
    steps = plan_redis_start(running=False, macos=False, off_box_host=None)
    assert steps.index(AwaitServerReady()) < steps.index(AffirmAcl())
    assert steps.index(RequireFreePort()) < steps.index(LaunchServer())
    assert steps.index(WriteConf()) < steps.index(LaunchServer())
