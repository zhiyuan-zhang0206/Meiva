"""The context-breakdown wire models and the response built from one request's breakdown.

`ContextBreakdownResponse` is the body of `GET /api/agents/{id}/context-breakdown` and the base of
the run timeline's per-request context. Both the gateway and the insights service build it, so the
models and the budget resolution (the agent's model window and compaction thresholds) live here
rather than in either of them.
"""

from loguru import logger

from base.agents.history.context_breakdown import (
    ContextBreakdownResponse as ContextBreakdownResponse,
)
from base.agents.history.context_breakdown import (
    ContextCategory as ContextCategory,
)
from base.agents.history.context_breakdown import (
    ContextSection as ContextSection,
)
from base.agents.history.context_breakdown import (
    RequestBreakdown,
)
from base.agents.model.model_overrides import read_agent_overrides
from base.db.protocols import SyncPool
from base.lm.catalog import ModelCatalog
from base.lm.context_budget import UnknownModelWindowError, resolve_context_budget
from base.lm.registry import resolve_available_model


def resolve_agent_model(
    pool: SyncPool,
    agent_id: int,
    *,
    catalog: ModelCatalog,
    default_model: str,
) -> str:
    """The agent's effective LLM model: its per-agent overlay, else the cluster default
    (the caller's live default), resolved through any withdrawal fallback so callers judge the model
    that will actually run. Capability gates (image input) must use this resolved id, never the raw
    configured one."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT config_overlay FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    overlay = row[0] if row and row[0] else None
    model = overlay.get("llm_model") if overlay else None
    return resolve_available_model(model or default_model, models=catalog.models)


def context_breakdown_response(
    pool: SyncPool,
    agent_id: int,
    breakdown: RequestBreakdown,
    *,
    catalog: ModelCatalog,
    default_model: str,
) -> ContextBreakdownResponse:
    """`breakdown` (one LLM request's input) with the agent's resolved window and compaction
    thresholds; the thresholds are 0 when the agent's model has no known window."""
    model = resolve_agent_model(pool, agent_id, catalog=catalog, default_model=default_model)
    overrides = read_agent_overrides(pool, agent_id)
    max_input_tokens = soft_compact_tokens = hard_compact_tokens = 0
    try:
        budget = resolve_context_budget(model, overrides, catalog=catalog)
        max_input_tokens = budget.max_context_tokens
        soft_compact_tokens = budget.soft_compact_tokens
        hard_compact_tokens = budget.hard_compact_tokens
    except UnknownModelWindowError as exc:
        logger.warning("context-breakdown: {}", exc)

    return ContextBreakdownResponse(
        total_input_tokens=breakdown.total.tokens,
        estimated=breakdown.total.estimated,
        exact_fraction=breakdown.total.exact_fraction,
        max_input_tokens=max_input_tokens,
        soft_compact_tokens=soft_compact_tokens,
        hard_compact_tokens=hard_compact_tokens,
        sections=[ContextSection.from_node(node) for node in breakdown.sections],
        categories=[
            ContextCategory(
                kind=c.kind,
                tokens=c.total.tokens,
                estimated=c.total.estimated,
                exact_fraction=c.total.exact_fraction,
            )
            for c in breakdown.categories
        ],
    )
