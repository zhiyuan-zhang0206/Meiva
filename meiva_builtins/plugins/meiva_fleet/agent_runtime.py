from __future__ import annotations

from base.api_contracts.agent_contract import Harness
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.lm.harness.handoff import EntryPoint, HarnessContribution
from base.packages.plugins.config_registration import get_plugin_config
from base.packages.plugins.extensions import PluginContributions
from meiva_builtins.plugins.meiva_fleet.default_config import FleetConfig

_FLEET_HARNESS_CONTRIBUTION = HarnessContribution(
    harnesses=frozenset({Harness.CLAUDE_CODE, Harness.CODEX, Harness.ANTIGRAVITY}),
    entries=(
        EntryPoint(
            "meiva.ui",
            "deliver through the configured user delivery protocol",
        ),
    ),
    overlaps=(
        "When you need the user to answer or decide, ask with `meiva.ui.ask` (see "
        "`meiva.help(meiva.ui)`) in preference to your CLI's own ask-user or question tool. "
        "Meiva shows the question to the user in Meiva, keeps it open until they answer and "
        "hands the answer back to you, while a question raised inside the CLI session is never "
        "seen by them.",
    ),
    discipline=(
        "Send assignments and progress with `meiva.agents.send_message`. When your work "
        "is complete, `meiva.self.terminate(message=...)` reports to your nearest living ancestor.",
    ),
)


def fleet_self_section(_slices: AgentSlices, *, catalog: ModelCatalog) -> str:
    del catalog
    if _slices.prompt.user_delivery_mode == "sdk":
        return (
            "## Fleet\n\n"
            "Send assignments and progress with `meiva.agents.send_message`. "
            "Use `meiva.ui.ask` for user decisions and apply the core user-attention test "
            "before publishing information with `meiva.ui.publish`."
        )
    return (
        "## Fleet\n\n"
        "Workflow selection belongs to `meiva-workflow`. For Fleet coordination, "
        "load `meiva-fleet` and its applicable operating contract.\n\n"
        "## Agent-to-agent communication\n\n"
        "Send assignments and progress directly to whoever must act with "
        "`meiva.agents.send_message`. Message another agent only when they must reply, "
        "decide, act, or know something to continue. Say what happened, what you need, "
        "and what breaks without it. Use one reporter per milestone. "
        "Act on received messages without courtesy ACKs; explicit reporting agreements still apply. "
        "Periodic checking does not imply periodic broadcasting. "
        "When your work is complete, `meiva.self.terminate(message=...)` reports the "
        "result to your nearest living ancestor. If a different agent delegated the work, "
        "send the result to them directly.\n\n"
        "Use schedules for timed checks and recurring work, GitHub Issues for repository bugs "
        "and CI follow-up, and workspace `MEMORY.md` for durable working context.\n\n"
        "Apply the core user-attention test before choosing user delivery. "
        "Deliver qualifying information through the configured protocol. "
        "User-only decisions reach the user directly. Ask for decisions with `meiva.ui.ask`. "
        "Text questions have one answer field; choices keep an optional Note. "
        "Questions persist until the user answers or explicitly closes them."
    )


def reduce_context_switch_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:
    del catalog
    if slices.prompt.user_delivery_mode == "sdk":
        return ""
    if not get_plugin_config("meiva_fleet", slices, FleetConfig).reduce_context_switch:
        return ""
    return (
        "## Reduce context switch for the human\n\n"
        "Queue, never push: out-of-band interruption is reserved for irreversible "
        "risk in motion, the whole effort blocked on the human, or an explicit "
        "request to be woken. Preserve each pending question; lack of "
        "an answer does not justify escalation. Load "
        "`reduce-context-switch-for-human` for reporting cadence, aggregation, "
        "and push-channel procedures when needed."
    )


def fleet_compact_reminder(_slices: AgentSlices, *, catalog: ModelCatalog) -> str:
    del catalog
    return (
        "If you are coordinating work, reconcile changed work status and record "
        "outstanding assignments, promised reports, blockers and artifact locations "
        "in your durable working notes. Keep any user-facing commitments current "
        "in the shared to-user document. Update existing records only when something "
        "changed; compaction alone is not a reason to send peers or the user a message."
    )


def contribute() -> PluginContributions:
    return PluginContributions(
        system_prompt_sections=(fleet_self_section, reduce_context_switch_section),
        compact_reminder_sections=(fleet_compact_reminder,),
        harness_contributions=(_FLEET_HARNESS_CONTRIBUTION,),
    )
