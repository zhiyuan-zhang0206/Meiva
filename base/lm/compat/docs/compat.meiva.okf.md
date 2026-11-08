---
type: doc
title: LLM Compatibility Layers
description: '`base/lm/compat/anthropic_thinking.py` and `openai_reasoning.py` — provider quirks folded into the chat classes, and `provider_sdks/` — the one entry into each provider SDK.'
tags:
- base
- library
- llm-inference
---

# LLM Compatibility Layers

- `anthropic_thinking.py`: `ThinkingTokensChatAnthropic` — ChatAnthropic subclass patching thinking_tokens into usage_metadata (base drops it); shared by claude/deepseek.
- `openai_reasoning.py`: `ReasoningContentChatModel` — ChatOpenAI subclass folding delta `reasoning_content` into canonical thinking blocks; **used by glm / mimo / qwen**. kimi uses `langchain-moonshot`; reasoning lands in `additional_kwargs["reasoning_content"]`, handled by fan-out + timeline.

## Provider SDK imports (`provider_sdks/`)

One module per model SDK — `anthropic_sdk.py`, `openai_sdk.py`, `genai_sdk.py` — each a single root
import (`import anthropic`, `import openai`, `from google import genai`) plus the names that package
exposes only through `__getattr__` (`genai.APIError`). Every other site reaches a provider SDK through
that module and at the package root, never through a submodule of its own.

The module body is the mechanism: the first thread holds that module's import lock until the SDK is
complete, so a second thread waits instead of reading a half-built package. It matters because
`agent_host` builds a runtime per turn while the same process runs other work on executor threads
(`asyncio.to_thread`, the ops pool). CPython waits for a second *root* import of a package, but not
for a *submodule* import: its parent is already in `sys.modules`, so it runs against a package whose
`__init__` is still executing, and the `from ... import name` behind it reads a half-built module.
On `company-mini` that dropped four hosted turns in 24h (`#6538`): `ImportError: cannot import name
'DEFAULT_MAX_RETRIES' from partially initialized module 'anthropic._base_client'`, and the same shape
for `Messages` from `anthropic.resources.beta`, both reached through
`anthropic_thinking.py`'s `from anthropic.types import ...` in a per-turn provider build.

A site that needs one symbol imports the owner and names it off that module
(`anthropic.APIError`, `genai.types.MediaResolution`); a new SDK gets its own module here and an entry
in this package's `exports`. `base/lm/tests/test_provider_sdk_imports.py` holds the regression: a cold
process per production path, a cold process running them from eight threads at once, and a scan that
refuses a new submodule import in `base/lm` or `meiva_builtins/plugins`.
