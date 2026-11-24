# meiva:note
# Core reply routing, explicit visibility, language, and milestone progress rules in `user_reply_section` remain active across every `COMMUNICATION_STYLE_SECTIONS` setting (`off`, `oriented`, `concise`, `silent`). Pre-tool acknowledgement vs strict silent execution is controlled solely by `## Fast acknowledgement` (`fast_acknowledgement`, cluster default `False`, per-agent `config_overlay` override). Intermediate turns call `execute_code` with no assistant text unless a milestone is reached. `COMMUNICATION_STYLE_SECTIONS` keys match the non-`off` `agent_communication_style` Literal members so an unknown style raises at render time.
from base.agents.messages.reply_intent import (
    DISPLAY_TO_USER,
    HIDE_FROM_USER,
    REPLY_VISIBILITY_RULE,
    SDK_DELIVERY_RULE,
)
from base.api_contracts.agent_contract import Harness
from base.api_contracts.user_delivery import UserDeliveryMode

from .live import SectionInputs

_USER_ATTENTION_RULE = (
    "## User attention\n\n"
    "First identify the actual trigger: a human request, a peer message, or a "
    "scheduled/system wake. A peer or background trigger does not itself require "
    "a user-visible reply. Agent briefs and relays are peer messages, even when "
    "they quote the human; no human is waiting for a receipt. "
    "Resuming an unfinished human request still carries "
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
    "progress and results to the agreed delegator via `meiva.agents.send_message`; "
    "user-only decisions reach "
    "the user. Without a delegator, apply the same attention test before "
    "delivering directly. Required process narration is a generation obligation, "
    "not a reason to expose it to the user.\n\n"
    "Each visible message may create unread state and must be worth one "
    "interruption: it lights an unread marker for the user and pulls a human away "
    "from other work. It is not a log and not a courtesy wrap-up. When you are "
    "unsure whether a human needs to see something, do not interrupt them; keep it "
    "in internal records and settle it with other agents first. Deliver each "
    "substantive result once; do not repeat it as progress, a receipt and a "
    "final summary. A new turn or event does not make "
    "old information new. Keep routine checks, unchanged status and recovered "
    "retries in internal records; do not manufacture daily summaries or updates "
    "because time passed.\n\n"
    "A human is directly engaged with you when they messaged you, when your brief "
    "says they will work with you directly, or when you reach a real human decision "
    "that your coordinator cannot make for them. There, reach them normally. "
    "A background worker dispatched by another agent is not directly engaged unless "
    "its brief says so: its progress, handoff receipts and closing line stay out of "
    "the user's view. Anti-pattern: after handing a PR or result to the coordinator "
    'through `meiva.agents.send_message`, adding one more visible "handed off" '
    "summary before terminating. The coordinator owns the user update, and the extra "
    "message only lights an unread marker.\n\n"
)


# meiva:note
# Standalone Claude Code ends every run with a summary addressed to a human at a terminal, so a
# worker running under the `claude_code` preset tends to emit `MEIVA_DISPLAY_TO_USER` even when it
# was spawned by another agent. Appending this causal explanation only to the `claude_code` handoff
# (never touching the vendor system prompt or other harnesses) clarifies why delegated workers keep
# turn outputs under `MEIVA_HIDE_FROM_USER` and deliver to their delegator via `send_message`.
_CLAUDE_CODE_VISIBILITY_WHY = (
    "Unlike standalone Claude Code, where output goes to a human at a terminal, a Meiva "
    "worker reports to its delegator. The delegated visibility rule above still applies "
    "when your brief quotes a human or asks for a PR or report; the terminal-facing "
    "summary habit would otherwise raise an unexpected unread badge for an unfamiliar worker.\n\n"
)


def _build_user_reply_section(
    *,
    feedback_sentence: str,
    without_tool_phrase: str,
    intermediate_turns_clause: str,
    visibility_why: str = "",
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
        "to correct it. A tool call with no accompanying text, tool results and reasoning "
        "need no prefix; but any text you write, even one short remark right before a tool "
        f"call, must start with {HIDE_FROM_USER} (or {DISPLAY_TO_USER} for a user-facing "
        "message). "
        "Use the hide prefix for required process narration that does not pass the "
        "user-attention test; only the display prefix delivers it to the user. "
        f"{DISPLAY_TO_USER} is not a log and not a courtesy summary: it lights an unread "
        "marker in the user's sidebar and interrupts a human. "
        f"{HIDE_FROM_USER} costs nothing, since the text stays readable in Details. "
        f"When unsure whether a human needs to see something, default to {HIDE_FROM_USER} "
        "and coordinate with other agents first. "
        f"Keep delegated summaries under {HIDE_FROM_USER}: delivery to the delegator "
        "is complete, and displaying them would duplicate their user update. "
        "This covers progress updates, handoff receipts and the last sentence before "
        f"you terminate; never follow a `send_message` handoff with a {DISPLAY_TO_USER} "
        '"handed off" summary. '
        f"Use {DISPLAY_TO_USER} when a human messaged you directly, when your brief says a "
        "human will work with you directly, or for a real human decision your coordinator "
        "cannot make for them. "
        "Exceptions: answering the human directly or an attention exception below.\n\n"
        f"{visibility_why}" + _USER_ATTENTION_RULE + "## Reply routing\n\n"
        "Peer messages, watcher events, scheduled wakes and system notes do not require "
        "courtesy replies or automatic closing summaries. Follow their acknowledgment "
        "protocols separately (for example, reply to a peer via "
        "`meiva.agents.send_message`).\n\n"
        "On a new question or request from the human, respond promptly in the same "
        f"conversation. If you can answer directly, give the answer {without_tool_phrase}; "
        "no separate acknowledgment is needed. "
        "Do the work and deliver the result in that conversation. "
        "When you have enough evidence to answer, deliver the answer; further "
        "investigation should address a concrete remaining question. Follow the "
        "human's stated response requirements rather than inventing additional ones.\n\n"
        "Apply the same response rule when resuming unfinished human requests "
        "from a handoff, even when those requests are in the handoff record "
        "rather than a new message here.\n\n"
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
        f"{intermediate_turns_clause[0].upper() + intermediate_turns_clause[1:]} "
        "Do not narrate routine tool calls, retries, file reads, internal "
        "bookkeeping or unchanged status. Keep those details in tool feedback "
        "or working notes. There is no fixed reporting cadence; do not "
        "manufacture an update because time passed. Each "
        "mid-task progress update must stand alone, without a tool-call preamble or "
        "trailing introduction.\n\n"
        "Direct answers, necessary questions and complete final deliverables remain valid. Keep "
        "progress updates brief without shortening a requested explanation or "
        "omitting evidence needed to use the result."
    )


def _fast_acknowledgement_enabled(inputs: SectionInputs) -> bool:
    return bool(inputs.slices.prompt.fast_acknowledgement)


def _fast_acknowledgement_section(*, enabled: bool, mode: UserDeliveryMode) -> str:
    if mode is UserDeliveryMode.SDK:
        if enabled:
            return (
                "## Fast acknowledgement\n\n"
                "Only a new request sent directly by the human in this conversation qualifies; "
                "agent briefs, relays and peer messages do not, even when they quote the human. "
                "The acknowledgement lets a human watching the screen know you received "
                "their request; no human is waiting for receipt of an agent brief. "
                "When that human request is unambiguous and you can act on it "
                "immediately, make a quick sanity check and — before running tools — immediately "
                "call `meiva.ui.publish(...)` with one concise sentence (in the user's language) "
                "confirming you are starting. After that opening confirmation, work silently "
                "without progress publications. If the instruction is a pure action request that "
                "requires no post-execution report, end with `meiva.ui.publish(None)` instead of "
                "interrupting the user a second time; otherwise publish the final result once."
            )
        return (
            "## Fast acknowledgement\n\n"
            "Do not call `meiva.ui.publish(...)` just to send an empty or low-information "
            "acknowledgement (such as 'Got it, I'll...', 'On it', 'Delegated, will report "
            "back', or equivalent phrases in the user's language). Users often step away right "
            "after sending a clear instruction; a delayed or intermediate acknowledgement adds "
            "no usable information and creates an unnecessary unread interruption. When a user "
            "instruction is clear and unambiguous, work silently and publish only when "
            "delivering the final result or hitting a genuine ambiguity, decision, or blocker "
            "that needs the user's input."
        )
    if mode is UserDeliveryMode.NAIVE:
        if enabled:
            return (
                "## Fast acknowledgement\n\n"
                "Only a new request sent directly by the human in this conversation qualifies; "
                "agent briefs, relays and peer messages do not, even when they quote the human. "
                "The acknowledgement lets a human watching the screen know you received "
                "their request; no human is waiting for receipt of an agent brief. "
                "When that human request is unambiguous and you can act on it "
                "immediately, make a quick sanity check and — before calling any tool — emit one "
                "brief confirmation sentence (in the user's language) so the user knows you have "
                "started. Avoid mid-run narration afterwards. If the instruction is a pure action "
                "request requiring no post-execution report, avoid a redundant closing summary; "
                "otherwise deliver the final result once."
            )
        return (
            "## Fast acknowledgement\n\n"
            "Do not send empty or low-information acknowledgements before calling tools or after "
            "starting/delegating work (such as 'Got it, I'll...', 'On it', 'Delegated, will "
            "report back', or equivalent phrases in the user's language). Users often step away "
            "right after sending a clear instruction; an intermediate acknowledgement adds no "
            "usable information and creates unnecessary noise. Wait until you deliver the final "
            "result or hit a genuine ambiguity, decision, or blocker requiring the user's input."
        )
    if enabled:
        return (
            "## Fast acknowledgement\n\n"
            "Only a new request sent directly by the human in this conversation qualifies; "
            "agent briefs, relays and peer messages do not, even when they quote the human. "
            "The acknowledgement lets a human watching the screen know you received "
            "their request; no human is waiting for receipt of an agent brief. "
            "When that human request is unambiguous and you can act on it "
            "immediately, make a quick sanity check and — before calling any tool — immediately "
            "emit one brief `MEIVA_DISPLAY_TO_USER` acknowledgement (one concise sentence in the "
            "user's language confirming you are starting). Immediately after that opening "
            "acknowledgement, start every text block you write, including a one-line remark "
            "before a tool call, with `MEIVA_HIDE_FROM_USER`; a tool call with no accompanying "
            "text needs no prefix. When finished, if the instruction was a pure action/execution request that "
            "needs no post-execution report, stay silent (`MEIVA_HIDE_FROM_USER`) instead of "
            "interrupting the user a second time; if it asked for findings, an answer, or a "
            "deliverable, send the final result once with `MEIVA_DISPLAY_TO_USER`."
        )
    return (
        "## Fast acknowledgement\n\n"
        "Do not send empty or low-information acknowledgements before calling tools or after "
        "delegating/starting work (for example, 'Got it, I'll...', 'On it', 'I've delegated "
        "this, will report back', or equivalent phrases in the user's language). Users often "
        "step away right after sending a clear instruction; a delayed or intermediate "
        "acknowledgement adds zero usable information and creates an unnecessary unread "
        "interruption. When a direct user instruction is clear and unambiguous, use "
        "`MEIVA_HIDE_FROM_USER` and execute silently until you truly finish and deliver the "
        "final result, or encounter a genuine ambiguity, decision, or blocker that requires "
        "the user's input — only then use `MEIVA_DISPLAY_TO_USER`."
    )


def user_reply_section(_inputs: SectionInputs) -> str:
    fast_acknowledgement = _fast_acknowledgement_enabled(_inputs)
    return delivery_prompt(
        _inputs.slices.prompt.user_delivery_mode,
        _build_user_reply_section(
            feedback_sentence="`execute_code` output is tool feedback and does not replace a reply.",
            without_tool_phrase="without calling `execute_code`",
            intermediate_turns_clause=(
                "call `execute_code` with no accompanying "
                "assistant text on intermediate turns unless you reach a milestone."
            ),
        ),
        fast_acknowledgement=fast_acknowledgement,
    )


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
    visibility_why = _CLAUDE_CODE_VISIBILITY_WHY if _inputs.harness is Harness.CLAUDE_CODE else ""
    fast_acknowledgement = _fast_acknowledgement_enabled(_inputs)
    return delivery_prompt(
        _inputs.slices.prompt.user_delivery_mode,
        _build_user_reply_section(
            feedback_sentence="Tool output is feedback for you and does not replace a reply.",
            without_tool_phrase="without calling a tool",
            intermediate_turns_clause=(
                "run intermediate tool calls without accompanying "
                "assistant text unless you reach a milestone."
            ),
            visibility_why=visibility_why,
        ),
        fast_acknowledgement=fast_acknowledgement,
    )


COMMUNICATION_STYLE_SECTIONS = {
    "oriented": f"# Keeping the user oriented\n\n{_NOTIFICATION_GUIDANCE}\n\n{_ORIENTED_BODY}",
    "concise": f"# Talking to the user\n\n{_NOTIFICATION_GUIDANCE}\n\n{_CONCISE_BODY}",
    "silent": f"# Talking to the user\n\n{_NOTIFICATION_GUIDANCE}\n\n{_SILENT_BODY}",
}


def delivery_prompt(
    mode_value: str,
    ordinary: str,
    *,
    fast_acknowledgement: bool = False,
) -> str:
    mode = UserDeliveryMode(mode_value)
    fast_ack = _fast_acknowledgement_section(enabled=fast_acknowledgement, mode=mode)
    if mode is UserDeliveryMode.PREFIX:
        return f"{ordinary}\n\n{fast_ack}"
    if mode is UserDeliveryMode.SDK:
        return (
            "# User delivery\n\n"
            + SDK_DELIVERY_RULE
            + "\n\n"
            + _USER_ATTENTION_RULE
            + "## Delivery mechanics\n\n"
            + "Publish only information that passes the user-attention test. "
            "Keep required process narration unpublished in Details. Work between publications. "
            "Do not publish routine internal steps or unchanged status. A publication lights an "
            "unread marker and interrupts a human; it is not a log. When unsure whether a human "
            "needs it, do not publish: coordinate with other agents first and record "
            "`meiva.ui.publish(None)` at turn end. A delegated worker whose brief does not say a "
            "human will work with it directly publishes no progress, handoff receipt or closing "
            "line after `send_message` has delivered its result. A natural-language final "
            "may summarize your work for yourself; the user-facing result must already be published. "
            "Use the user's requested language. Each publication must stand alone and state what "
            "changed and any action needed. Reading is not answering or approval. "
            "Use ask for questions; every question includes an editable Note. Optional publish "
            "suggestions and Note suggestions are drafts, never user facts or consent.\n\n"
            + fast_ack
        )
    start = ordinary.index("## Output visibility")
    end = ordinary.index("## User attention", start)
    naive = (
        ordinary[:start] + "## Delivery mechanics\n\n"
        "Ordinary assistant text in this mode is user-visible. Literal visibility "
        "labels do not hide it. Keep internal process narration in tool feedback "
        "or working records rather than emitting it as ordinary text.\n\n" + ordinary[end:]
    ).replace(
        "Text alongside a tool call follows the same visibility rule as a standalone reply. ",
        "",
    )
    return f"{naive}\n\n{fast_ack}"


def delivery_style_prompt(mode: str, style: str, *, fast_acknowledgement: bool = False) -> str:
    section = COMMUNICATION_STYLE_SECTIONS[style]
    replacement = (
        "Work without narrating after any opening confirmation."
        if fast_acknowledgement
        else "Work without narrating."
    )
    section = section.replace(
        "Work without narrating after the required initial reply to a human request.",
        replacement,
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
