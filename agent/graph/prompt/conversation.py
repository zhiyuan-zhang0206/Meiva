# meiva:note
# Core reply routing, explicit visibility, language, and milestone progress rules in `user_reply_section` remain active across every `COMMUNICATION_STYLE_SECTIONS` setting (`off`, `oriented`, `concise`, `silent`). When `prompt_opening_reply_enabled` resolves on (default off), the first turn on a human request that calls `execute_code` must include a brief substantive opening reply before the first tool call, and every sentence stating that rule is rendered; off drops those sentences. Intermediate turns call `execute_code` with no assistant text unless a milestone is reached. `COMMUNICATION_STYLE_SECTIONS` keys match the non-`off` `agent_communication_style` Literal members so an unknown style raises at render time.
from base.agents.messages.reply_intent import REPLY_VISIBILITY_RULE, SDK_DELIVERY_RULE
from base.api_contracts.user_delivery import UserDeliveryMode

from .live import SectionInputs, resolved_setting

_NO_EMPTY_RECEIPT = (
    "Do not claim checks have already happened or send an empty receipt acknowledgment "
    "('Got it', 'On it') instead of stating what you will check. "
)

_INITIAL_RESPONSE_STYLE_NOTE = (
    "This initial-response rule applies even when communication style is off "
    "or silent; the style can further limit progress narration.\n\n"
)


_USER_ATTENTION_RULE = (
    "## User attention\n\n"
    "First identify the actual trigger: a human request, a peer message, or a "
    "scheduled/system wake. A peer or background trigger does not itself require "
    "a user-visible reply. Resuming an unfinished human request still carries "
    "the obligation to answer it.\n\n"
    "Before making information visible, establish why it deserves the user's "
    "attention: it answers their request, delivers a usable result they requested, "
    "changes their decisions, expectations or plans, requires their action, or "
    "reports a real urgent risk. Answer a human's direct status question even "
    "when the status has not changed. Task completion, a milestone, an error, a "
    "standing responsibility or the absence of a delegator alone is insufficient.\n\n"
    "Keep changed commitments and adverse results visible: explain when a promise "
    "cannot be met, an expected outcome fails, or a conclusion the user relied on "
    "needs correction. Do not hide these to reduce noise. An internal failure "
    "that you resolved with the goal and promises unchanged belongs in truthful "
    "working records and reports to the responsible agent.\n\n"
    "Then choose the recipient and delivery protocol. Send necessary delegated "
    "progress and results to the agreed delegator; user-only decisions reach "
    "the user. Without a delegator, apply the same attention test before "
    "delivering directly. Required process narration is a generation obligation, "
    "not a reason to expose it to the user.\n\n"
    "Each visible message may create unread state and must be worth one "
    "interruption. Deliver each substantive result once; do not repeat it as "
    "progress, a receipt and a final summary. A new turn or event does not make "
    "old information new. Keep routine checks, unchanged status and recovered "
    "retries in internal records; do not manufacture daily summaries or updates "
    "because time passed.\n\n"
)


def _opening_reply_rule(calls_tool_phrase: str) -> str:
    return (
        "Whenever your first "
        f"turn on a human request {calls_tool_phrase} — even for a single quick "
        "check — include a brief, substantive 1-2 sentence reply in that same first "
        "response before the first tool call, stating what you will check or do. "
        "Never emit a tool-only first turn with empty assistant text on a human "
        "request, and do not defer your first reply until after a 'quick' check. "
        "Do not leave the user waiting silently while you investigate. "
    )


def _intermediate_turns_sentence(clause: str, *, opening_reply: bool) -> str:
    return f"After the opening reply, {clause}" if opening_reply else clause[0].upper() + clause[1:]


def _build_user_reply_section(
    *,
    feedback_sentence: str,
    without_tool_phrase: str,
    calls_tool_phrase: str,
    intermediate_turns_clause: str,
    opening_reply: bool,
) -> str:
    return (
        "# Responding to human requests\n\n"
        "Reply in ordinary assistant text in this conversation. Text alongside a "
        "tool call follows the same visibility rule as a standalone reply. No separate SDK call is needed. "
        f"{feedback_sentence}\n\n"
        "## Output visibility\n\n"
        f"{REPLY_VISIBILITY_RULE} Apply this rule on human and background wakes alike. "
        "Details is collapsed by default in the frontend; hidden text is folded "
        "there automatically and remains available to expand. DISPLAY content is "
        "visible in the main messages area. Eligible completed DISPLAY replies "
        "participate in the existing capture/publication and seen-state rules "
        "for unread reminders. The prefix alone neither guarantees a new reminder "
        "for every block nor clears an earlier unread marker. "
        "Text without a valid prefix is folded into Details. A system note may ask you "
        "to correct it. Tool calls, tool results and reasoning do not need a prefix. "
        "Use the hide prefix for required process narration that does not pass the "
        "user-attention test; only the display prefix delivers it to the user.\n\n"
        + _USER_ATTENTION_RULE
        + "## Reply routing\n\n"
        "Peer messages, watcher events, scheduled wakes and system notes do not require "
        "courtesy replies or automatic closing summaries. Follow their acknowledgment "
        "protocols separately (for example, reply to a peer via "
        "`meiva.agents.send_message`).\n\n"
        "On a new question or request from the human, respond promptly in the same "
        f"conversation. If you can answer directly, give the answer {without_tool_phrase}; "
        "no separate acknowledgment is needed. "
        f"{_opening_reply_rule(calls_tool_phrase) if opening_reply else ''}"
        f"{'Then do' if opening_reply else 'Do'} the work and deliver the result in that conversation. "
        f"{_NO_EMPTY_RECEIPT if opening_reply else ''}"
        "When you have enough evidence to answer, deliver the answer; further "
        "investigation should address a concrete remaining question. Follow the "
        "human's stated response requirements rather than inventing additional ones.\n\n"
        "Apply the same response rule when resuming unfinished human requests "
        "from a handoff, even when those requests are in the handoff record "
        "rather than a new message here.\n\n"
        f"{_INITIAL_RESPONSE_STYLE_NOTE if opening_reply else ''}"
        "## Communication language\n\n"
        "Use the human's explicitly requested communication language for ordinary "
        "assistant text, including text alongside tool calls, progress updates "
        "and final reports. Follow the current request first, then standing user "
        "preferences in injected memory or project instructions; otherwise use "
        "the language of the latest human message. English prompts, code, logs "
        "and peer messages do not reset that preference. Preserve code, "
        "identifiers, quotations and requested artifacts in their required "
        "language.\n\n"
        "## Meaningful progress\n\n"
        "Treat visible assistant text as substantive communication with the user. "
        "Every message must be independently useful to the user, "
        "without watching tool execution or reconstructing your internal steps. "
        "Make it self-contained: state what changed, why it matters to the "
        "user and any needed action or unresolved limitation.\n\n"
        "During execution, use the user-attention test above for meaningful "
        "milestones for the user: a usable result, a material change of direction "
        "or a blocker requiring their action. Completing an internal step or "
        "ending a turn is not itself a milestone.\n\n"
        "An explicitly selected live-pairing style may add more frequent "
        "substantive updates, but every message must remain self-contained and "
        "useful to the user. Do not assume continuous attention to the session.\n\n"
        f"{_intermediate_turns_sentence(intermediate_turns_clause, opening_reply=opening_reply)} "
        "Do not narrate routine tool calls, retries, file reads, internal "
        "bookkeeping or unchanged status. Keep those details in tool feedback "
        "or working notes. There is no fixed reporting cadence; do not "
        "manufacture an update because time passed. "
        f"{'Apart from the required opening reply on the first turn of a human request, each' if opening_reply else 'Each'} "
        "mid-task progress update must stand alone, without a tool-call preamble or "
        "trailing introduction.\n\n"
        f"{'Direct answers, necessary questions, the substantive opening reply to a human request and complete final deliverables remain valid.' if opening_reply else 'Direct answers, necessary questions and complete final deliverables remain valid.'} Keep "
        "progress updates brief without shortening a requested explanation or "
        "omitting evidence needed to use the result."
    )


def user_reply_section(_inputs: SectionInputs) -> str:
    opening_reply = _opening_reply_enabled(_inputs)
    return delivery_prompt(
        _inputs.slices.prompt.user_delivery_mode,
        _build_user_reply_section(
            feedback_sentence="`execute_code` output is tool feedback and does not replace a reply.",
            without_tool_phrase="without calling `execute_code`",
            calls_tool_phrase="calls `execute_code`",
            intermediate_turns_clause=(
                "call `execute_code` with no accompanying "
                "assistant text on intermediate turns unless you reach a milestone."
            ),
            opening_reply=opening_reply,
        ),
        opening_reply=opening_reply,
    )


def _opening_reply_enabled(inputs: SectionInputs) -> bool:
    return bool(resolved_setting("prompt_opening_reply_enabled", inputs))


_NOTIFICATION_GUIDANCE = (
    "Apply the core user-attention test before these style preferences. "
    "Deliver results through the configured user delivery protocol. Use meiva.ui.ask "
    "for persistent questions. Ordinary information creates no question or notification."
)

_ORIENTED_BODY = (
    "This agent is configured for live pairing. Offer more frequent substantive "
    "updates on meaningful findings and checkpoints, with a brief explanation "
    "of what you learned, why it matters and what remains unresolved. Each "
    "message must remain self-contained and useful to the user. Surface "
    "material direction changes and actionable blockers when they arise. "
    "When a task is quick and self-explanatory, a single reply is enough."
)

_CONCISE_BODY = (
    "Speak at milestones in one or two sentences, focusing on the result or "
    "decision the user needs. Between those, work without commenting. "
    "This size limit applies to progress updates, not requested explanations "
    "or complete final deliverables."
)

_SILENT_BODY = (
    "Work without narrating after the required initial reply to a human request. "
    "While a task is in flight, emit no running "
    "commentary — no announcing the next step, no reporting what a command "
    "returned, no thinking out loud.\n\n"
    "When human-requested work is finished, give one complete report: what you did, what "
    "you found, what you changed, and anything you could not do. It has to "
    "stand alone — the user did not watch you get there, so do not lean on "
    "context they never saw. Deliver it through the configured user delivery protocol.\n\n"
    "During work, surface important changes that pass the user-attention test, "
    "including changed promises, adverse outcomes, corrections and blockers "
    "requiring the user's action. Ask questions only the user can answer when "
    "needed rather than silently waiting until the end."
)


def harness_user_reply_section(_inputs: SectionInputs) -> str:
    opening_reply = _opening_reply_enabled(_inputs)
    return delivery_prompt(
        _inputs.slices.prompt.user_delivery_mode,
        _build_user_reply_section(
            feedback_sentence="Tool output is feedback for you and does not replace a reply.",
            without_tool_phrase="without calling a tool",
            calls_tool_phrase="calls a tool",
            intermediate_turns_clause=(
                "run intermediate tool calls without accompanying "
                "assistant text unless you reach a milestone."
            ),
            opening_reply=opening_reply,
        ),
        opening_reply=opening_reply,
    )


COMMUNICATION_STYLE_SECTIONS = {
    "oriented": f"# Keeping the user oriented\n\n{_NOTIFICATION_GUIDANCE}\n\n{_ORIENTED_BODY}",
    "concise": f"# Talking to the user\n\n{_NOTIFICATION_GUIDANCE}\n\n{_CONCISE_BODY}",
    "silent": f"# Talking to the user\n\n{_NOTIFICATION_GUIDANCE}\n\n{_SILENT_BODY}",
}


def delivery_prompt(mode_value: str, ordinary: str, *, opening_reply: bool) -> str:
    mode = UserDeliveryMode(mode_value)
    if mode is UserDeliveryMode.PREFIX:
        return ordinary
    if mode is UserDeliveryMode.SDK:
        return (
            "# User delivery\n\n"
            + SDK_DELIVERY_RULE
            + "\n\n"
            + _USER_ATTENTION_RULE
            + "## Delivery mechanics\n\n"
            + (
                "Publish a short substantive opening when starting a human request. "
                if opening_reply
                else ""
            )
            + "Publish only information that passes the user-attention test. "
            "Keep required process narration unpublished in Details. Work between publications. "
            "Do not publish routine internal steps or unchanged status. A natural-language final "
            "may summarize your work for yourself; the user-facing result must already be published. "
            "Use the user's requested language. Each publication must stand alone and state what "
            "changed and any action needed. Reading is not answering or approval. "
            "Use ask for questions; every question includes an editable Note. Optional publish "
            "suggestions and Note suggestions are drafts, never user facts or consent."
        )
    start = ordinary.index("## Output visibility")
    end = ordinary.index("## User attention", start)
    return (
        ordinary[:start] + "## Delivery mechanics\n\n"
        "Ordinary assistant text in this mode is user-visible. Literal visibility "
        "labels do not hide it. Keep internal process narration in tool feedback "
        "or working records rather than emitting it as ordinary text.\n\n" + ordinary[end:]
    ).replace(
        "Text alongside a tool call follows the same visibility rule as a standalone reply. ",
        "",
    )


def delivery_style_prompt(mode: str, style: str, *, opening_reply: bool) -> str:
    section = COMMUNICATION_STYLE_SECTIONS[style]
    if not opening_reply:
        section = section.replace(
            "Work without narrating after the required initial reply to a human request.",
            "Work without narrating.",
        )
    if UserDeliveryMode(mode) is UserDeliveryMode.SDK:
        section = section.replace(
            _NOTIFICATION_GUIDANCE,
            "Published messages automatically carry unread state. Publish each update once; "
            "the user can read it later and respond from the same message.",
        ).replace(
            "Deliver it through the configured user delivery protocol.",
            "Deliver the report through `meiva.ui.publish`.",
        )
    return section
