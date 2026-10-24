# meiva:note
# The CodeAct section (`codeact_section`) is owned as its own module so `system_prompt.py` stays within its line ceiling while preserving explicit registration order. When `prompt_codeact_enabled` is on, it directs the agent to batch known operations into one `execute_code(code: str)` call when no intermediate review is needed.
from .live import SectionInputs


def codeact_section(inputs: SectionInputs) -> str:
    if not inputs.slices.prompt.prompt_codeact_enabled:
        return ""
    return (
        "# CodeAct — batch work into fewer calls\n\n"
        "Use one `execute_code(code: str)` call for several known operations "
        "when you do not need to inspect an intermediate result:\n\n"
        "- Read independent files together.\n"
        "- Use ordinary if-else logic for branches whose conditions and "
        "actions are already understood.\n"
        "- Combine fetch, transform and write when the transformation is "
        "known and no intermediate review is required."
    )
