from __future__ import annotations

from collections.abc import Mapping

from base.host.env.registry import process_environment

# meiva:note
# The variables the codex binary itself reads for key material (verified against the
# shipped binary's strings: OPENAI_API_KEY, CODEX_API_KEY, CODEX_ACCESS_TOKEN). Anything
# else in the environment is the caller's business and rides along unchanged — the
# runner's home allowlist and the session's registration handle reach the child that way.
# meiva:link base/lm/harness/codex/docs/codex.meiva.okf.md
SECRET_ENV_NAMES: tuple[str, ...] = ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN")


# meiva:note
# The codex counterpart of `base.lm.harness.claude.auth`. The CLI prefers a key found in
# its environment over the ChatGPT login in `~/.codex/auth.json`, so a cluster-pinned key
# inherited from the parent process silently moved every agent onto per-token billing
# with no plan quota. Removal is what keeps that key out, not an empty value: this CLI
# asks whether the variable is *present*, and an empty string is still present (the
# Claude CLI treats an empty value as absent, which is why that adapter blanks instead).
# `api_key_mode` is the one explicit choice of per-token billing; only then do the
# variables ride along.
# meiva:link base/lm/harness/codex/docs/codex.meiva.okf.md
def build_env(environ: Mapping[str, str], *, api_key_mode: bool = False) -> dict[str, str]:
    env = dict(environ)
    if api_key_mode:
        return env
    for name in SECRET_ENV_NAMES:
        env.pop(name, None)
    return env


def session_env(environ: Mapping[str, str] | None, *, api_key_mode: bool) -> dict[str, str]:
    return build_env(
        process_environment() if environ is None else environ, api_key_mode=api_key_mode
    )
