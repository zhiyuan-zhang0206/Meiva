from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, Protocol, Self, TypedDict

from base.host.runtime_interpreter import external_plugin_read_root
from base.log import logger
from base.packages.docs.frontmatter import FrontmatterError, parse_frontmatter
from base.packages.plugins.flags import FlagDomainUnavailable, read_declared_flag
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
    try:
        return bool(read_declared_flag("agent.commands_enabled", ("agent.commands_enabled",)))
    except FlagDomainUnavailable:
        from base.host.env.dotenv_file import process_env_file, read_env_aliases

        value = read_env_aliases(process_env_file()).get("MEIVA_COMMANDS_ENABLED")
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


@dataclass(frozen=True)
class CommandSources:
    builtin_commands: Path
    user_commands: Path
    builtin_plugins: Path
    external_plugins: Path

    @classmethod
    def system(cls) -> Self:
        return cls(
            builtin_commands=repo_root() / "meiva_builtins" / "commands",
            user_commands=meiva_home() / "commands",
            builtin_plugins=repo_plugins_dir(),
            external_plugins=external_plugin_read_root(),
        )


class CommandEnvironment(Protocol):
    def enabled(self) -> bool: ...

    def sources(self) -> CommandSources: ...


class _SystemCommandEnvironment:
    def enabled(self) -> bool:
        return _commands_enabled()

    def sources(self) -> CommandSources:
        return CommandSources.system()


# meiva:link base/packages/docs/composer-commands.meiva.okf.md
def command_dirs(
    skills: Sequence[Skill], *, sources: CommandSources
) -> list[tuple[Path, tuple[str, ...]]]:
    dirs: list[tuple[Path, tuple[str, ...]]] = [
        (sources.builtin_commands, ()),
        (sources.user_commands, ()),
    ]
    for base in (sources.builtin_plugins, sources.external_plugins):
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
def discover_commands(skills: Sequence[Skill], *, environment: CommandEnvironment) -> list[Command]:
    if not environment.enabled():
        return []
    out: dict[str, Command] = skill_commands(skills)
    for d, base_ns in command_dirs(skills, sources=environment.sources()):
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
    environment: CommandEnvironment

    @classmethod
    def system(cls, *, skills: Callable[[], Sequence[Skill]]) -> Self:
        return cls(skills=skills, environment=_SystemCommandEnvironment())

    def discover(self) -> list[Command]:
        if not self.environment.enabled():
            return []
        return discover_commands(self.skills(), environment=self.environment)

    def expand(self, content: str) -> str:
        if not content.startswith("/"):
            return content
        return expand_command(content, self.discover())
