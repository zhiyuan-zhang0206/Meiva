from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, TypedDict

from base.config import settings
from base.host.runtime_interpreter import external_plugin_read_root
from base.log import logger
from base.packages.docs.frontmatter import FrontmatterError, parse_frontmatter
from base.packages.skills import scan_tree
from base.packages.skills.names import display_name, match_key
from base.packages.skills.tree import Skill, flatten, identifier, target
from base.paths import meiva_home, repo_plugins_dir, repo_root, skills_dir


# meiva:link base/packages/docs/composer-commands.meiva.okf.md
def load_skill_names(provider_roots: Sequence[Path] = ()) -> list[Skill]:
    from base.packages.extensions.install_registry import loadable_skill_names

    return flatten(scan_tree(skills_dir(meiva_home()), loadable_skill_names(), provider_roots))


# meiva:link base/packages/docs/composer-commands.meiva.okf.md
def _commands_enabled() -> bool:
    if settings.has_domain("agent"):
        return settings.agent.commands_enabled
    from base.host.env.dotenv_file import read_env_aliases

    value = read_env_aliases().get("MEIVA_COMMANDS_ENABLED")
    if value is None:
        return True
    return value.strip().lower() in {"1", "true", "yes", "on"}


# meiva:link base/packages/docs/composer-commands.meiva.okf.md
class Command(TypedDict):
    name: str
    description: str
    instruction_hint: str
    body: str
    skill_target: str | None


# meiva:link base/packages/docs/composer-commands.meiva.okf.md
def command_dirs(
    skills: Sequence[Skill],
    *,
    root: Path | None = None,
    home: Path | None = None,
    repo_plugins: Path | None = None,
    external_plugins: Path | None = None,
) -> list[tuple[Path, tuple[str, ...]]]:
    resolved_root = repo_root() if root is None else root
    resolved_home = meiva_home() if home is None else home
    dirs: list[tuple[Path, tuple[str, ...]]] = [
        (resolved_root / "meiva_builtins" / "commands", ()),
        (resolved_home / "commands", ()),
    ]
    r_plugins = repo_plugins_dir() if repo_plugins is None else repo_plugins
    e_plugins = external_plugin_read_root() if external_plugins is None else external_plugins
    for base in (r_plugins, e_plugins):
        if base.is_dir():
            dirs.extend(
                (p / "commands", (p.name,))
                for p in sorted(base.iterdir())
                if p.is_dir() and not p.name.startswith((".", "_"))
            )
    for sk in skills:
        dirs.append((Path(sk["path"]) / "commands", (*sk["namespace"], sk["name"])))
    return dirs


# meiva:link base/packages/docs/composer-commands.meiva.okf.md
def scan_dir(d: Path, base_ns: tuple[str, ...]) -> dict[str, Command]:
    out: dict[str, Command] = {}
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.md")):
        content = f.read_text(encoding="utf-8")
        if content.startswith("---\n"):
            try:
                fields, body = parse_frontmatter(content)
            except FrontmatterError as e:
                logger.warning("[commands] skip malformed {f}: {e}", f=f, e=e)
                continue
        else:
            fields, body = {}, content

        name = ":".join(display_name(seg) for seg in (*base_ns, f.stem))
        out[name] = {
            "name": name,
            "description": fields.get("description", ""),
            "instruction_hint": fields.get("instruction-hint") or fields.get("argument-hint", ""),
            "body": body.strip(),
            "skill_target": None,
        }
    return out


# meiva:link base/packages/docs/composer-commands.meiva.okf.md
def skill_commands(skills: Sequence[Skill]) -> dict[str, Command]:
    out: dict[str, Command] = {}
    for sk in skills:
        ident = identifier(sk)
        out[ident] = {
            "name": ident,
            "description": sk["description"],
            "instruction_hint": "",
            "body": "",
            "skill_target": target(sk),
        }
    return out


# meiva:link base/packages/docs/composer-commands.meiva.okf.md
def discover_commands(
    skills: Sequence[Skill],
    *,
    dirs: Sequence[tuple[Path, tuple[str, ...]]] | None = None,
    enabled: bool | None = None,
) -> list[Command]:
    if not (_commands_enabled() if enabled is None else enabled):
        return []
    out: dict[str, Command] = skill_commands(skills)
    resolved_dirs = command_dirs(skills) if dirs is None else dirs
    for d, base_ns in resolved_dirs:
        out.update(scan_dir(d, base_ns))
    return [out[name] for name in sorted(out)]


# meiva:link base/packages/docs/composer-commands.meiva.okf.md
class Invocation(NamedTuple):
    command: Command
    typed_name: str
    argument: str


_INVOCATION = re.compile(r"(?:\A|(?<=\s))/(\S+)")


# meiva:link base/packages/docs/composer-commands.meiva.okf.md
def split_commands(content: str, commands: Sequence[Command]) -> list[Invocation] | None:
    catalog = {match_key(c["name"]): c for c in commands}
    hits = [
        (m.start(), m.group(1), catalog[match_key(m.group(1))])
        for m in _INVOCATION.finditer(content)
        if match_key(m.group(1)) in catalog
    ]
    if not hits or hits[0][0] != 0:
        return None
    chain: list[Invocation] = []
    for i, (start, typed, cmd) in enumerate(hits):
        end = hits[i + 1][0] if i + 1 < len(hits) else len(content)
        chain.append(Invocation(cmd, typed, content[start + 1 + len(typed) : end].strip()))
    return chain


# meiva:link base/packages/docs/composer-commands.meiva.okf.md
def _expand_one(inv: Invocation) -> str:
    name = inv.typed_name
    if inv.command["skill_target"]:
        out = (
            f"/{name} invokes the {name} skill. Load it with "
            f"meiva.help(meiva.skills.{inv.command['skill_target']}) and follow it for this task."
        )
    else:
        out = f"Command /{name}:\n{inv.command['body']}"
    if inv.argument:
        out += f"\nAdditional message: {inv.argument}"
    return out


# meiva:link base/packages/docs/composer-commands.meiva.okf.md
def expand_command(content: str, commands: Sequence[Command]) -> str:
    if not content.startswith("/"):
        return content
    chain = split_commands(content, commands)
    if chain is None:
        return content
    return "\n\n".join(_expand_one(inv) for inv in chain)


# meiva:link base/packages/docs/composer-commands.meiva.okf.md
@dataclass(frozen=True)
class CommandCatalog:
    skills: Callable[[], Sequence[Skill]]
    dirs: Sequence[tuple[Path, tuple[str, ...]]] | None = None
    enabled: bool | Callable[[], bool] | None = None

    def _is_enabled(self) -> bool:
        if self.enabled is None:
            return _commands_enabled()
        return self.enabled() if callable(self.enabled) else self.enabled

    def discover(self) -> list[Command]:
        if not self._is_enabled():
            return []
        if self.dirs is None and self.enabled is None:
            return discover_commands(self.skills())
        return discover_commands(self.skills(), dirs=self.dirs, enabled=True)

    def expand(self, content: str) -> str:
        if not content.startswith("/"):
            return content
        return expand_command(content, self.discover())
