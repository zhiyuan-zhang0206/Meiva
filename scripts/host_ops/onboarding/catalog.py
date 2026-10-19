from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

Rotation = Literal["static", "rotating", "unknown"]
InstallKind = Literal["script", "npm"]


@dataclass(frozen=True)
class Store:
    path: str
    rotation: Rotation
    why: str


@dataclass(frozen=True)
class Installer:
    kind: InstallKind
    target: str
    pin_template: str = ""

    @property
    def pins(self) -> bool:
        return bool(self.pin_template)


@dataclass(frozen=True)
class HarnessSpec:
    cli: str
    runtime: str | None
    binary: str
    pinned_version: str
    installer: Installer
    config: tuple[str, ...]
    stores: tuple[Store, ...]
    needs_node: bool = False


# meiva:note
# One row per harness: the vendor command that installs it, the files that carry
# its configuration, and the stores that carry its login. `rotation` is the only
# reason the store table exists — it says whether copying a login invalidates the
# machine it came from, and `sync` withholds every store whose answer is not
# `static`. The measurements behind each verdict live in the runbook.
# meiva:link meiva_builtins/skills/platform/meiva-guide/deploy/references/harness-distribution.md
CATALOG: dict[str, HarnessSpec] = {
    "claude": HarnessSpec(
        cli="claude",
        runtime="claude_code",
        binary="claude",
        pinned_version="2.1.290",
        installer=Installer(
            kind="script",
            target="https://claude.ai/install.sh",
            pin_template="{version}",
        ),
        config=("~/.claude/settings.json", "~/.claude/CLAUDE.md"),
        stores=(
            Store(
                path="~/.claude/.credentials.json",
                rotation="unknown",
                why=(
                    "OAuth access+refresh pair. macOS keeps the authoritative copy in the "
                    "login Keychain (service 'Claude Code-credentials') and falls back to "
                    "this file when the item is absent; Linux uses the file only."
                ),
            ),
        ),
    ),
    "codex": HarnessSpec(
        cli="codex",
        runtime="codex",
        binary="codex",
        pinned_version="0.163.0",
        installer=Installer(kind="npm", target="@openai/codex", pin_template="{version}"),
        config=("~/.codex/config.toml", "~/.codex/AGENTS.md"),
        stores=(
            Store(
                path="~/.codex/auth.json",
                rotation="unknown",
                why=(
                    "auth_mode='chatgpt' carries tokens.{id_token,access_token,refresh_token}; "
                    "auth_mode='apikey' carries a static OPENAI_API_KEY. Only the first can go "
                    "stale, which is why sync classifies this store by its content."
                ),
            ),
        ),
        needs_node=True,
    ),
    "agy": HarnessSpec(
        cli="agy",
        runtime="antigravity",
        binary="agy",
        pinned_version="1.4.2",
        installer=Installer(
            kind="script",
            target="https://antigravity.google/cli/install.sh",
            pin_template="",
        ),
        config=(
            "~/.gemini/antigravity-cli/settings.json",
            "~/.gemini/config/mcp_config.json",
        ),
        stores=(
            Store(
                path="~/.gemini/antigravity-cli/antigravity-oauth-token",
                rotation="unknown",
                why=(
                    "Google OAuth token blob read by the CLI at start-up (observed with lsof on "
                    "a live agy process). Stores token.access_token / token.refresh_token / "
                    "token.expiry."
                ),
            ),
            Store(
                path="~/.gemini/jetski-standalone-oauth-token",
                rotation="unknown",
                why=(
                    "The older location of the same blob, still present on machines whose agy "
                    "was installed before the CLI split its state directory."
                ),
            ),
        ),
    ),
    "dsh": HarnessSpec(
        cli="dsh",
        runtime=None,
        binary="dsh",
        pinned_version="0.1.5-rc.3",
        installer=Installer(kind="npm", target="@deepseek-ai/dsh", pin_template="{version}"),
        config=("${DSH_HOME:-~/.dsh}/settings.yaml",),
        stores=(
            Store(
                path="${DSH_HOME:-~/.dsh}/.credentials.yaml",
                rotation="static",
                why=(
                    "A DeepSeek API key the CLI reads from disk (refs: DEEPSEEK_API_KEY). A key "
                    "is not rotated by being used, so a copy does not invalidate the original."
                ),
            ),
        ),
        needs_node=True,
    ),
}

CLI_NAMES: tuple[str, ...] = tuple(CATALOG)


def spec(cli: str) -> HarnessSpec:
    try:
        return CATALOG[cli]
    except KeyError:
        legal = ", ".join(CLI_NAMES)
        raise ValueError(f"unknown harness {cli!r}; choose one of: {legal}") from None


def install_command(harness: HarnessSpec, version: str | None = None) -> str:
    wanted = harness.pinned_version if version is None else version
    if harness.installer.kind == "npm":
        return f"npm install -g {harness.installer.target}@{wanted}"
    if not harness.installer.pin_template:
        return f"curl -fsSL {harness.installer.target} | bash"
    args = harness.installer.pin_template.format(version=wanted)
    return f"curl -fsSL {harness.installer.target} | bash -s -- {args}"


def credential_risk(harness: HarnessSpec, path: str, content: bytes) -> Rotation:
    if harness.cli != "codex":
        return _rotation_of(harness, path)
    try:
        parsed = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _rotation_of(harness, path)
    if not isinstance(parsed, dict):
        return _rotation_of(harness, path)
    if parsed.get("auth_mode") == "apikey" or parsed.get("OPENAI_API_KEY"):
        return "static"
    return _rotation_of(harness, path)


def _rotation_of(harness: HarnessSpec, path: str) -> Rotation:
    for store in harness.stores:
        if store.path == path:
            return store.rotation
    raise ValueError(f"{harness.cli} has no store at {path!r}")
