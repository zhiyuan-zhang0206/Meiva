# meiva:note
# Who owns the connections of an attachment. An independent attachment boots its own clients from
# its configuration and releases every one of them when it detaches or fails to start; a borrowed
# attachment uses the prior context's database factory and never closes the prior clients; neither
# redelivers the environment. The ownership is observed on the real resources: the event pipeline
# the owned client set built, and the Postgres backends the owned and the prior connections hold.
# meiva:link meiva/impersonation/docs/attachment.meiva.okf.md

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import meiva
from agent.tests.impersonation.external.runtime import Takeover, independent_boot
from base.agents.context.clients import ClientSet
from base.config import ConfigBoot
from base.host.proc import run_bounded
from base.telemetry.delivery.pipeline import EventPipeline
from base.telemetry.delivery.receipts import Event
from meiva import impersonation
from meiva.boot import ExternalBoot
from meiva.impersonation.admission import lease_target
from meiva.impersonation.attachment import AttachInputs, Attachment
from meiva.sdk_surface import install, process_context

BOUND_S = 15.0
MISSING_LEASE = "00000000-0000-0000-0000-000000000000"


def discard(batch: list[Event]) -> None:
    del batch


# meiva:note
# A boot whose client set owns an observable event pipeline, built before the attachment.
def owned_boot() -> tuple[ExternalBoot, EventPipeline]:
    boot = independent_boot()
    pipeline = EventPipeline(writer=discard, flush_interval_s=0.01)
    clients = ClientSet(database=boot.database, pipeline_factory=lambda: pipeline)
    clients.event_pipeline()
    return replace(boot, clients=clients), pipeline


def backend_alive(takeover: Takeover, pid: int) -> bool:
    row = takeover.cluster.db_conn.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE pid=%s", (pid,)
    ).fetchone()
    takeover.cluster.db_conn.commit()
    return row is not None and row[0] > 0


def wait_until_gone(takeover: Takeover, pid: int) -> bool:
    deadline = time.monotonic() + BOUND_S
    while time.monotonic() < deadline:
        if not backend_alive(takeover, pid):
            return True
        time.sleep(0.02)
    return False


def test_an_independent_attachment_closes_the_clients_it_owns_at_detach(
    attached_runtime: Takeover,
) -> None:
    process_context.unbind_context()
    boot, pipeline = owned_boot()
    target = lease_target(attached_runtime.lease_id, None)
    with Attachment(AttachInputs(target, None, boot)):
        (pid,) = meiva.context.sql.execute("SELECT pg_backend_pid()").fetchone()
        assert backend_alive(attached_runtime, pid)
        assert not pipeline.stopped
    assert pipeline.stopped
    assert wait_until_gone(attached_runtime, pid)
    assert getattr(meiva, "context", None) is None


def test_a_borrowed_attachment_never_closes_the_prior_clients(attached_runtime: Takeover) -> None:
    prior = meiva.context
    (before,) = prior.sql.execute("SELECT pg_backend_pid()").fetchone()
    with impersonation.attach(attached_runtime.lease_id):
        assert meiva.context.clients is prior.clients
    assert meiva.context is prior
    (after,) = prior.sql.execute("SELECT pg_backend_pid()").fetchone()
    assert after == before


def test_read_only_attachment_refuses_an_undelivered_login_before_dial(tmp_path: Path) -> None:
    authority = tmp_path / "db-authority"
    authority.mkdir()
    (authority / "ledger.json").write_text("{}", encoding="utf-8")
    environment = {key: value for key, value in os.environ.items() if key != "MEIVA_AGENT_ID"}
    result = run_bounded(
        [sys.executable, str(Path(__file__).with_name("child_undelivered_login.py"))],
        env={
            **environment,
            "MEIVA_HOME": str(tmp_path),
            "MEIVA_CONFIG_FETCH": "skip",
            "MEIVA_DB_URL": "postgresql://127.0.0.1:1/undelivered",
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def assert_competing_attachment_is_refused(lease_id: str) -> None:
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(impersonation.attach, lease_id)
        with pytest.raises(RuntimeError, match="already has an external attachment"):
            future.result(timeout=5)


@pytest.mark.parametrize("contended", [False, True], ids=["sequential", "contended"])
def test_independent_attachment_does_not_redeliver_environment(
    attached_runtime: Takeover, contended: bool
) -> None:
    before = dict(os.environ)
    delivered = ConfigBoot()
    delivered.read_process_environment()
    process_context.unbind_context()
    target = lease_target(attached_runtime.lease_id, None)
    with Attachment(AttachInputs(target, None, independent_boot())):
        assert dict(os.environ) == before
        assert meiva.context.require_clock().timezone == delivered.view.general.timezone
        if contended:
            assert_competing_attachment_is_refused(attached_runtime.lease_id)
        assert dict(os.environ) == before
    assert dict(os.environ) == before


@pytest.mark.parametrize("failure_at", ["lease", "plugins", "snapshot"])
def test_unbound_attachment_failure_closes_owned_clients_and_releases_binding(
    attached_runtime: Takeover, failure_at: str
) -> None:
    process_context.unbind_context()
    boot, pipeline = owned_boot()
    primary = RuntimeError("the surface never loaded")
    lease_id = attached_runtime.lease_id
    if failure_at == "lease":
        lease_id = MISSING_LEASE
    elif failure_at == "snapshot":
        attached_runtime.drop_checkpoint()
    else:
        install.uninstall()
        install.mark_load_failed(primary)
    try:
        with pytest.raises(RuntimeError) as raised:
            Attachment(AttachInputs(lease_target(lease_id, None), None, boot))
        if failure_at == "plugins":
            assert raised.value is primary
    finally:
        install.clear_load_attempt()
    assert pipeline.stopped
    assert getattr(meiva, "context", None) is None
    with pytest.raises(RuntimeError, match="does not exist"):
        impersonation.attach(MISSING_LEASE)


def test_the_facade_closes_the_clients_it_built_when_construction_is_refused(
    attached_runtime: Takeover,
) -> None:
    held: Any = impersonation.attach(attached_runtime.lease_id)
    try:
        with pytest.raises(RuntimeError, match="already has an external attachment"):
            impersonation.attach(attached_runtime.lease_id)
    finally:
        held.close()
