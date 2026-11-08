# meiva:note
# The provider SDKs' first import, run as a cold process: `routes` proves every production path
# enters each SDK through `base.lm.compat.provider_sdks`, `race` runs those same paths from several threads
# at once. `test_provider_sdk_imports.py` runs it and reads the exit status.
# meiva:link base/lm/compat/docs/compat.meiva.okf.md

from __future__ import annotations

import sys
import threading
import traceback

# meiva:note
# The production first-touch paths, each repeated so several threads ask for the same SDK at once:
# the per-turn provider build that crashed on company-mini (`lm_deepseek.build` imports this compat
# module) and the failure-path imports of `base/lm/errors.py`, which reach all three SDKs. The Google
# provider's own entry is not raced here: `base` may not import `meiva_builtins`, and its import site
# is covered by `test_provider_sdk_imports.py`.
_ENTRIES = (
    "anthropic_thinking",
    "openai_reasoning",
    "classify_error",
) * 2


# meiva:note
# Every module the entries import before they reach an SDK, loaded first so that nothing but the
# SDK import stands between a thread's start and that import: a thread that spends its first
# milliseconds importing a shared parent enters the SDK after the others left it, and the race is
# never run.
def _warm_up() -> None:
    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatResult

    from base.host.boundary.untyped import object_of
    from base.lm import compat
    from base.lm.errors import classify_error

    warmed = (object_of, compat, classify_error, AIMessageChunk, ChatResult)
    assert all(loaded is not None for loaded in warmed)


def _first_touch(name: str) -> None:
    if name == "anthropic_thinking":
        from base.lm.compat.anthropic_thinking import ThinkingTokensChatAnthropic

        assert ThinkingTokensChatAnthropic is not None
    elif name == "openai_reasoning":
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        assert ReasoningContentChatModel is not None
    else:
        from base.lm.errors import classify_error

        _ = classify_error(RuntimeError("probe"))


# meiva:note
# The names the production tracebacks named were missing because the package was still executing
# elsewhere; asking for them here is what a caller needs from an SDK the owners finished importing.
# The module-level names are read from `vars()`: they are the SDK's own internals, not a surface
# this repository may type against.
def _require_complete_sdks() -> None:
    import anthropic
    import openai
    from google import genai

    assert anthropic.APIError is not None and openai.APIError is not None
    assert genai.types.MediaResolution is not None
    assert "DEFAULT_MAX_RETRIES" in vars(sys.modules["anthropic._base_client"])
    assert "Messages" in vars(sys.modules["anthropic.resources.beta"])
    assert "APIError" in vars(sys.modules["google.genai.errors"])


def _require_owner(name: str) -> None:
    qualified = f"base.lm.compat.provider_sdks.{name}"
    assert qualified in sys.modules, f"{qualified} did not import this SDK"


# meiva:note
# The failure path imports every SDK, so it runs first and each owner is asked for on its own; the
# two compat modules then run the paths that crashed in production, and the SDKs must still be
# complete after them.
def _routes() -> int:
    _first_touch("classify_error")
    _require_owner("anthropic_sdk")
    _require_owner("openai_sdk")
    _require_owner("genai_sdk")
    _first_touch("anthropic_thinking")
    _first_touch("openai_reasoning")
    _require_complete_sdks()
    return 0


def _race() -> int:
    _warm_up()
    failures: list[str] = []
    barrier = threading.Barrier(len(_ENTRIES))

    def run(name: str) -> None:
        _ = barrier.wait()
        try:
            _first_touch(name)
        except BaseException:
            failures.append(f"{name}: {traceback.format_exc()}")

    threads = [threading.Thread(target=run, args=(name,)) for name in _ENTRIES]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for failure in failures:
        _ = sys.stderr.write(failure)
    _require_complete_sdks()
    return 1 if failures else 0


def _main(argv: list[str]) -> int:
    scenario = argv[1] if len(argv) > 1 else ""
    if scenario == "routes":
        return _routes()
    if scenario == "race":
        return _race()
    _ = sys.stderr.write(f"unknown scenario {scenario!r}\n")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
