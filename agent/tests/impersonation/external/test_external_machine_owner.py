# meiva:note
# Which machine owner an attachment carries. A borrowed attachment keeps the prior context's exact
# owner and its field caches; an independent one boots its own owner from its configuration, and
# two independent attachments never share one.
# meiva:link meiva/impersonation/docs/attachment.meiva.okf.md

from __future__ import annotations

import os

import meiva
from agent.tests.impersonation.external.runtime import Takeover, independent_boot
from base.config.tests.configuration import data_plane_environment
from meiva import impersonation
from meiva.impersonation.admission import lease_target
from meiva.impersonation.attachment import AttachInputs, Attachment
from meiva.sdk_surface import process_context


def test_attachment_borrows_the_prior_machine_without_resetting_its_cache(
    attached_runtime: Takeover,
) -> None:
    prior = meiva.context
    machine = prior.require_machine()
    assert machine.name() == attached_runtime.machine
    with impersonation.attach(attached_runtime.lease_id):
        assert meiva.context.require_machine() is machine
    assert meiva.context is prior
    assert prior.require_machine() is machine
    assert machine.name() == attached_runtime.machine


def test_independent_attachments_own_fresh_machine_caches(attached_runtime: Takeover) -> None:
    process_context.unbind_context()
    target = lease_target(attached_runtime.lease_id, None)
    owners: list[object] = []
    for _ in range(2):
        boot = independent_boot(data_plane_environment())
        with Attachment(AttachInputs(target, None, boot, pid=os.getpid(), suffix="probe")):
            machine = meiva.context.require_machine()
            assert machine is boot.machine
            assert machine.name() == attached_runtime.machine
            assert all(machine is not previous for previous in owners)
            owners.append(machine)
        assert getattr(meiva, "context", None) is None
