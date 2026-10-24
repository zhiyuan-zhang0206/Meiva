# meiva:note
# The words of the data plane's status, stop and remote bring-up output.
#
# Every line is a function of the reading it reports; nothing here probes a service.

from __future__ import annotations

from cli.commands.data_plane.instance import stop_plan
from cli.commands.data_plane.instance.bringup_lines import (
    remote_announce,
    remote_pg_failure,
    remote_ready,
    remote_redis_failure,
)
from cli.commands.data_plane.instance.status_lines import (
    failure_detail,
    local_pg_line,
    local_redis_line,
    pooler_line,
    probe_line,
    remote_status_header,
)


def test_a_probe_line_carries_the_reading_as_its_mark() -> None:
    assert probe_line(ok=True, detail="redis (h:1)") == "  ✓ redis (h:1)"
    assert probe_line(ok=False, detail="redis (h:1)") == "  ✗ redis (h:1)"


def test_a_stopped_postgres_is_unreachable() -> None:
    line = local_pg_line(host="127.0.0.1", port=5433, running=False, connect_error=None)
    assert line == "  ✗ postgres (127.0.0.1:5433) unreachable"


def test_a_server_that_refuses_the_login_is_never_a_green_tick() -> None:
    line = local_pg_line(
        host="127.0.0.1", port=5433, running=True, connect_error="password authentication failed"
    )
    assert line == (
        "  ✗ postgres (127.0.0.1:5433) reachable but connect failed: password authentication failed"
    )


def test_a_server_that_answers_the_pooled_login_is_green() -> None:
    line = local_pg_line(host="127.0.0.1", port=5433, running=True, connect_error=None)
    assert line == "  ✓ postgres (127.0.0.1:5433)"


def test_the_redis_line_names_the_dialled_endpoint() -> None:
    assert local_redis_line(host="10.0.0.7", port=6380, ok=True) == "  ✓ redis (10.0.0.7:6380)"
    assert local_redis_line(host="10.0.0.7", port=6380, ok=False) == "  ✗ redis (10.0.0.7:6380)"


def test_the_pooler_line_shows_the_registry_port_and_never_a_false_zero() -> None:
    assert pooler_line(port=6433, ok=True, refusal=None) == (
        "  ✓ pgbouncer (127.0.0.1:6433, transaction pooling)"
    )
    unresolved = pooler_line(port=None, ok=False, refusal=None)
    assert "no registry record" in unresolved and ":0" not in unresolved


def test_a_refused_operator_entry_is_said_instead_of_a_down_pooler() -> None:
    assert pooler_line(port=6433, ok=False, refusal="operator entry unreadable") == (
        "  ✗ pgbouncer (127.0.0.1:6433): operator entry unreadable"
    )


def test_the_remote_status_header_says_no_local_instance_is_managed() -> None:
    assert "remote-managed" in remote_status_header()
    assert "no local instance" in remote_status_header()


def test_a_failure_detail_is_the_last_non_empty_line() -> None:
    assert failure_detail("first\n\n  second  \n\n") == "second"
    assert failure_detail("") == "connection failed"
    assert failure_detail("\n  \n") == "connection failed"


def test_a_stop_plan_closes_the_pooler_before_postgres_before_redis() -> None:
    assert stop_plan.plan_stop(redis_port=6380) == (
        stop_plan.StopPooler(),
        stop_plan.StopPostgres(),
        stop_plan.StopRedis(6380),
    )


def test_a_url_that_names_no_redis_port_leaves_redis_alone() -> None:
    assert stop_plan.plan_stop(redis_port=None) == (
        stop_plan.StopPooler(),
        stop_plan.StopPostgres(),
    )


def test_a_remote_stop_says_there_is_nothing_to_stop_locally() -> None:
    notice = stop_plan.remote_stop_notice("10.9.8.7")
    assert "remote-managed (10.9.8.7)" in notice and "nothing to stop locally" in notice


def test_no_leftover_means_no_orphan_warning() -> None:
    assert stop_plan.orphan_warning(postgres_running=False, redis_listening=False) is None


def test_every_leftover_is_named_in_the_orphan_warning() -> None:
    both = stop_plan.orphan_warning(postgres_running=True, redis_listening=True)
    assert both is not None
    assert "local postgres + redis from before the switch is still running" in both
    assert "no longer managed" in both
    only_pg = stop_plan.orphan_warning(postgres_running=True, redis_listening=False)
    assert only_pg is not None and "local postgres from before" in only_pg


def test_redis_stopped_only_when_it_no_longer_answers() -> None:
    assert stop_plan.redis_stop_failure(6399, still_answers=False) is None
    failure = stop_plan.redis_stop_failure(6399, still_answers=True)
    assert failure == "redis still answers on 127.0.0.1:6399 after shutdown"


def test_the_remote_bring_up_words_carry_the_dial_detail() -> None:
    assert "remote-managed (10.9.8.7)" in remote_announce("10.9.8.7")
    failure = remote_pg_failure("postgres (10.9.8.7:5432) connect failed: timeout")
    assert (
        "remote data plane unreachable: postgres (10.9.8.7:5432) connect failed: timeout" in failure
    )
    assert "MEIVA_DB_URL" in failure and "pg_dump" in failure
    assert remote_redis_failure("redis (h:1) connect failed: x") == (
        "  ✗ remote data plane unreachable: redis (h:1) connect failed: x"
    )
    assert remote_ready("postgres (h:1)", "redis (h:2)") == (
        "  ✓ remote data plane reachable (postgres (h:1); redis (h:2))"
    )
