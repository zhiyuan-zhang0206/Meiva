"""Label generation logic — LLM call to auto-generate a short name for an agent.

Extracted from gateway/labels.py to eliminate the services -> gateway
reverse dependency.
"""

from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger

from base.agents.labels import publish_label_updated
from base.db import Database
from base.events.live.bus import EventBus
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_keys import ProviderKeys
from base.lm.content import message_content
from base.lm.errors import is_retryable_provider_error, normalize_provider_transport_error
from base.lm.factory import build_chat_model
from services.derived.labeler import label_rules
from services.derived.labeler.config import LabelerConfig


# meiva:note
# Generate a label via the LLM, CAS-write to DB, publish the event.
#
# Returns True when a label was written; False for a trusted transient
# provider-call failure or an empty/rejected label — the caller leaves the label unset;
# None when CAS skipped a user-owned label or one already set by another
# writer. An empty string is treated as unset only when it is not user-owned.
#
# Unknown and permanent provider failures propagate unchanged, as do model
# construction, usage, parsing and DB errors. Only the invocation boundary
# normalizes raw HTTP transport failures for the shared retry classification.
# A committed label is not rolled back or selected again if its best-effort
# live notification fails. Cancellation always propagates.
async def generate_label_async(
    agent_id: int,
    prompt: str,
    config: LabelerConfig,
    db: Database,
    bus: EventBus,
    *,
    catalog: ModelCatalog,
    keys: ProviderKeys,
    llm_override: str,
    overrides: ModelOverrides,
) -> bool | None:

    # thinking is both slow and expensive on the label path, and it
    # turns response.content into a list-of-blocks, adding consumer
    # parsing complexity (PR #69 hit a thinking block signature
    # leaking into the label). Disable at the source so the consumer
    # typically only needs to handle str content.
    from base.lm.usage.metering import unmeter

    llm = unmeter(
        build_chat_model(
            config.labeler_model,
            thinking={"type": "disabled"},
            catalog=catalog,
            keys=keys,
            llm_override=llm_override,
            overrides=overrides,
        )
    )
    messages = [
        SystemMessage(content=label_rules.system_prompt(config.labeler_max_chars)),
        HumanMessage(content=f"<user_request>{prompt}</user_request>"),
    ]
    try:
        response = await llm.ainvoke(messages)
    except Exception as exc:
        provider_error = normalize_provider_transport_error(exc)
        if not is_retryable_provider_error(provider_error):
            raise
        logger.opt(exception=True).warning(
            "label provider call failed for agent {agent_id}: {err}",
            event="label_generate_failed",
            agent_id=agent_id,
            err=repr(exc),
        )
        return False

    from base.lm.usage import log_usage_from_message

    log_usage_from_message(
        response,
        catalog=catalog,
        model=config.labeler_model,
        usage_kind="batch",
        for_agent_id=agent_id,
    )
    judgment = label_rules.judge(message_content(response), config.labeler_max_chars)
    if isinstance(judgment, label_rules.Empty):
        logger.error(
            "label generate produced empty string for agent {agent_id} (raw={raw!r})",
            event="label_generate_empty",
            agent_id=agent_id,
            raw=judgment.raw,
        )
        return False
    if isinstance(judgment, label_rules.Rejected):
        # Not a label — a failed generation. Returning False hands it to the
        # daemon's failure observation path instead of writing the
        # model's answer into a user-facing field (issue #178).
        logger.error(
            "label generate rejected for agent {agent_id} ({reason}): {label!r}",
            event="label_generate_rejected",
            agent_id=agent_id,
            reason=judgment.reason,
            label=judgment.label,
        )
        return False
    label = judgment.label

    # CAS: if the user manually PATCHed (in any direction — set or reset
    # back to NULL), label_user_set sticky bit flips to true and the
    # WHERE here does not match. spawn defaults to label NULL +
    # label_user_set false; the LLM write matches that. Legacy
    # 'agent-user' / 'thread-N' placeholder rows are non-empty and also
    # do not match (no special migration needed for old rows).
    #
    # Empty string is treated as the same "unset" state as NULL: a stray ''
    # (whatever wrote it) must be overwritable or the agent is wedged out of
    # auto-labeling forever — the daemon poll matches the same predicate, so
    # an '' row is re-selected and this CAS replaces it. This function never
    # writes '' itself (the `if not label` guard above returns False first).
    with db.write_transaction() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agents SET label=%s WHERE id=%s "
            "AND (label IS NULL OR label = '') AND NOT label_user_set",
            (label, agent_id),
        )
        rowcount = cur.rowcount
        conn.commit()
    if rowcount == 1:
        await publish_label_updated(bus, agent_id, label)
        logger.info(
            "label generated for agent {agent_id}: {label!r}",
            event="label_generated",
            agent_id=agent_id,
            label=label,
        )
        return True
    logger.info(
        "label generate skipped for agent {agent_id} (label was already set)",
        event="label_generate_skipped",
        agent_id=agent_id,
    )
    return None
