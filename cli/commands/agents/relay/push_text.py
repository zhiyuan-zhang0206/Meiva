# meiva:note
# The texts a relay pushes into the host: the exact ACK command, the self-contained envelope
# for one batch of durable inbox rows, and the start hint for a lease accepted without a
# briefing.
#
# Every envelope carries the full content, the batch's ids and the ACK command, so the
# external session processes messages without reading or parsing the inbox.
# meiva:link docs/conventions/agents/agent-impersonation-hosts.md

from __future__ import annotations

import shlex
from collections.abc import Sequence
from uuid import UUID

from cli.commands.agents.relay.inbox import DeliveryBudget, InboxMessage, LeaseRef

__all__ = ["ack_command", "activation_hint", "message_push"]

_TRUNCATED = " (truncated — run the inbox command to read the full body before acknowledging)"


# meiva:note
# The exact ACK command for one pushed batch, as a bare `meiva`: the host's
# `~/.local/bin/meiva`, linked to the production CLI.
def ack_command(lease_ref: int | UUID, ids: Sequence[int], agent_id: int | None = None) -> str:
    return shlex.join(
        [
            "meiva",
            "impersonate",
            "ack",
            str(lease_ref),
            *map(str, ids),
            *(["--agent", str(agent_id)] if isinstance(lease_ref, int) else []),
        ]
    )


# meiva:note
# One self-contained envelope covering one batch of durable inbox rows.
#
# A re-delivery push says so explicitly; the ids make it idempotent for a host that already
# received the batch. `max_chars` bounds each content block to fit Claude Monitor's per-line
# budget and limit host context; the tail points at the inbox command for the full body, to
# be read before the ACK.
def message_push(
    ref: LeaseRef,
    messages: Sequence[InboxMessage],
    *,
    budget: DeliveryBudget,
    redelivery: bool,
    max_chars: int | None,
) -> str:
    ids = [message.id for message in messages]
    header = (
        f"Meiva message agent={ref.agent_id} lease={ref.lease_ref} ids={','.join(map(str, ids))}"
    )
    if redelivery:
        header += " (re-delivery: unacknowledged)"
    blocks: list[str] = [header]
    for message in messages:
        content = message.content
        if max_chars is not None and len(content) > max_chars:
            content = content[:max_chars] + _TRUNCATED
        blocks.append(f"[id={message.id}] kind={message.kind} from={message.source}")
        blocks.append(
            f"Delivery attempt {message.delivery_attempts + 1}/{budget.max_delivery_attempts}"
        )
        blocks.append(content)
        blocks.append("")
    blocks.append(
        f"ACK after receiving this message: {ack_command(ref.lease_ref, ids, ref.agent_id)}"
    )
    blocks.append(
        f"ACK within {budget.ack_window_seconds}s. "
        + (
            "This batch includes a final delivery; a missed ACK window pauses automatic delivery of those messages."
            if any(m.delivery_attempts + 1 >= budget.max_delivery_attempts for m in messages)
            else f"At most {budget.max_delivery_attempts} total attempts per message; "
            "a missed final ACK window pauses automatic delivery; inbox reads and ACK remain valid."
        )
    )
    return "\n".join(blocks)


# meiva:note
# The fallback start message for a lease accepted without one.
#
# New accepts require a nonempty start message, so this only serves leases accepted before
# the push protocol shipped. It states the push contract: messages arrive here, acknowledge
# each batch on receipt, the inbox is the fallback read.
def activation_hint(ref: LeaseRef, budget: DeliveryBudget) -> str:
    prefix = ["meiva", "impersonate"]
    scope = ["--agent", str(ref.agent_id)] if isinstance(ref.lease_ref, int) else []
    inbox = shlex.join([*prefix, "inbox", str(ref.lease_ref), *scope])
    ack = shlex.join([*prefix, "ack", str(ref.lease_ref), *scope])
    return (
        f"Meiva control active: agent={ref.agent_id} lease={ref.lease_ref}. "
        "Inbox messages are pushed to this session; acknowledge each batch as soon as it "
        f"arrives: {ack} ID... Use {inbox} to read missed messages or a truncated "
        "message's full body — read it before acknowledging. "
        f"Each message has {budget.max_delivery_attempts} delivery attempts, "
        f"with {budget.ack_window_seconds}s to ACK each; exhaustion pauses message delivery, not the lease."
    )
